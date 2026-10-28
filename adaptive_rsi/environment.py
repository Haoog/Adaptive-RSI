import hashlib
import os
import shutil
import stat
from pathlib import Path

from .gitops import git
from .process import scrub_tree


def capture(source, destination, config):
    """Freeze explicitly selected, untracked dependencies outside candidate write access."""
    source, destination = Path(source).resolve(), Path(destination)
    destination.mkdir(parents=True)
    maximum = config["environment"]["max_seed_mb"] * 1024 * 1024
    size, digest = 0, hashlib.sha256(b"adaptive-rsi-dependencies-v2\0")
    def field(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    for relative in config["environment"]["seed_paths"]:
        path = (source / relative).resolve()
        path.relative_to(source)
        if not path.is_dir():
            raise RuntimeError("Dependency seed directory missing: " + relative)
        if git(source, "ls-files", "--", relative):
            raise RuntimeError("Seed only untracked dependencies; tracked files already enter the Git snapshot: " + relative)
        scrub_tree(path)
        def bounded_copy(src, dst):
            nonlocal size
            if not stat.S_ISREG(os.stat(src, follow_symlinks=False).st_mode):
                raise RuntimeError("Dependency seed must contain regular files")
            with open(src, "rb") as reader, open(dst, "xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > maximum:
                        raise RuntimeError("Dependency snapshot size limit exceeded")
                    writer.write(chunk)
            shutil.copystat(src, dst)
            return dst
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target, symlinks=True, copy_function=bounded_copy)
        # Hash the frozen copy, with framed fields and filesystem semantics.
        scrub_tree(target)
        for item in [target] + sorted(target.rglob("*")):
            info = item.lstat()
            field(item.relative_to(destination).as_posix().encode("utf-8", "surrogateescape"))
            field(str(stat.S_IMODE(info.st_mode)).encode())
            if item.is_symlink():
                field(b"link")
                field(os.readlink(item).encode("utf-8", "surrogateescape"))
            elif item.is_dir():
                field(b"directory")
            elif stat.S_ISREG(info.st_mode):
                field(b"file")
                field(str(info.st_size).encode())
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise RuntimeError("Dependency seed must contain regular files")
    return {"bytes": size, "sha256": digest.hexdigest()}


def prepare_cached(source, cache, config, tools):
    """Cache host-owned dependency seeds by complete inputs; restore always copies.

    This deliberately does not replay setup commands or network operations. The
    caller must supply an exact tool identity and explicitly selected seed paths.
    """
    import json
    import tempfile
    from .config import fingerprint
    from .evaluator import MANIFESTS
    source, cache = Path(source).resolve(), Path(cache).resolve()
    if cache == source or source in cache.parents:
        raise ValueError("Prepared cache must be outside the source tree")
    if not tools or not config["environment"]["seed_paths"]:
        raise ValueError("Prepared cache requires exact tools and explicit dependency seeds")
    cache.mkdir(parents=True, exist_ok=True)
    def tree_digest(root):
        digest = hashlib.sha256()
        scrub_tree(root)
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            data = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(info.st_mode),
                    "type": "link" if path.is_symlink() else "dir" if path.is_dir() else "file"}
            digest.update(json.dumps(data, sort_keys=True).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
        return digest.hexdigest()
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=cache) as temporary:
        temporary = Path(temporary)
        seeds = temporary / "seeds"
        seed = capture(source, seeds, config)
        manifests = {}
        # Include nested package manifests and lockfiles; never traverse selected
        # dependencies or symlinked directories looking for host inputs.
        for parent, directories, files in os.walk(source, followlinks=False):
            directories[:] = [name for name in directories if name != ".git" and
                               not (Path(parent) / name).is_symlink() and
                               not any((Path(parent) / name).relative_to(source).as_posix() == p.rstrip("/")
                                       for p in config["environment"]["seed_paths"])]
            for name in files:
                path = Path(parent) / name
                if name in MANIFESTS:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Cache manifests must be regular files")
                    manifests[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        inputs = {"version": 1, "tools": tools, "environment": config["environment"],
                  "sandbox": config["sandbox"], "manifests": manifests, "seed": seed}
        key = fingerprint(inputs)
        target = cache / key
        if target.exists():
            metadata = json.loads((target / "manifest.json").read_text())
            if metadata.get("key") != key or metadata.get("digest") != tree_digest(target / "seeds"):
                raise ValueError("Prepared environment cache integrity mismatch")
            return {"key": key, "path": str(target / "seeds"), "reused": True, "inputs": inputs}
        for path in sorted(seeds.rglob("*"), reverse=True) + [seeds]:
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        metadata = {"key": key, "digest": tree_digest(seeds), "inputs": inputs}
        (temporary / "manifest.json").write_text(json.dumps(metadata, sort_keys=True))
        # Rename a completed immutable tree atomically; concurrent identical
        # builders may reuse the winner only after a full integrity check.
        staging = cache / ("sealed-" + temporary.name)
        shutil.copytree(temporary, staging, symlinks=True)
        try:
            staging.rename(target)
        except OSError:
            if not target.exists():
                raise
            for path in staging.rglob("*"):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
            shutil.rmtree(staging)
            winner = json.loads((target / "manifest.json").read_text())
            if winner.get("key") != key or winner.get("digest") != tree_digest(target / "seeds"):
                raise ValueError("Prepared environment cache integrity mismatch")
        # TemporaryDirectory must be able to remove the private build copy.
        for path in seeds.rglob("*"):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        seeds.chmod(seeds.stat().st_mode | stat.S_IWUSR)
        return {"key": key, "path": str(target / "seeds"), "reused": False, "inputs": inputs}


def restore(source, target, config):
    if not source:
        return
    for relative in config["environment"]["seed_paths"]:
        destination = Path(target) / relative
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Candidate/seed dependency overlap: " + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(source) / relative, destination, symlinks=True)
        # Cache storage is immutable; each workspace owns an independently writable
        # copy. Never hardlink dependency files between tasks.
        for path in [destination] + list(destination.rglob("*")):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        info = Path(target) / ".git/info/exclude"
        if info.exists():
            marker = "/" + relative.rstrip("/") + "/"
            with info.open("r+") as stream:
                # 重复 restore 不再向 exclude 追加重复条目。
                if marker not in stream.read():
                    stream.write("\n" + marker + "\n")
