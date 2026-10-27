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


def restore(source, target, config):
    if not source:
        return
    for relative in config["environment"]["seed_paths"]:
        destination = Path(target) / relative
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("Candidate/seed dependency overlap: " + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(source) / relative, destination, symlinks=True)
        info = Path(target) / ".git/info/exclude"
        if info.exists():
            marker = "/" + relative.rstrip("/") + "/"
            with info.open("r+") as stream:
                # 重复 restore 不再向 exclude 追加重复条目。
                if marker not in stream.read():
                    stream.write("\n" + marker + "\n")
