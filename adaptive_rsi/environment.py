import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from .gitops import git
from .process import scrub_tree

# Sealed entries are immutable, so eviction only ever removes copies no run is
# reading. materialize() holds the entry's shared flock for the whole copy and
# prune() takes the same lock exclusively without waiting, so an idle window is
# no longer the only thing standing between an eviction and a live copy.
CACHE_ENTRY_LIMIT = 32
CACHE_BYTE_LIMIT = 4 * 1024 * 1024 * 1024
CACHE_IDLE_SECONDS = 24 * 60 * 60
STAGING_MAX_AGE_SECONDS = 24 * 60 * 60
SEED_DIGEST_PREFIX = b"adaptive-rsi-dependencies-v2\0"


def _seed_root(source, relative):
    """Validate one selected seed directory and return its resolved root."""
    path = (source / relative).resolve()
    path.relative_to(source)
    if not path.is_dir():
        raise RuntimeError("Dependency seed directory missing: " + relative)
    if git(source, "ls-files", "--", relative):
        raise RuntimeError("Seed only untracked dependencies; tracked files already enter the Git snapshot: " + relative)
    scrub_tree(path)
    return path


def _seed_list(path, relative):
    """Seed entries with the framed paths used for both seed digests."""
    base = Path(relative)
    entries = [(path, base.as_posix())]
    entries.extend((item, (base / item.relative_to(path)).as_posix()) for item in sorted(path.rglob("*")))
    return entries


def _seed_identity(digest, entries, maximum, used=0):
    """Feed framed path/mode/type/content identity for seed entries; returns bytes read."""
    def field(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    for path, framed in entries:
        info = path.lstat()
        field(framed.encode("utf-8", "surrogateescape"))
        field(str(stat.S_IMODE(info.st_mode)).encode())
        if path.is_symlink():
            field(b"link")
            field(os.readlink(path).encode("utf-8", "surrogateescape"))
        elif path.is_dir():
            field(b"directory")
        elif stat.S_ISREG(info.st_mode):
            field(b"file")
            field(str(info.st_size).encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    used += len(chunk)
                    if used > maximum:
                        raise RuntimeError("Dependency snapshot size limit exceeded")
                    digest.update(chunk)
        else:
            raise RuntimeError("Dependency seed must contain regular files")
    return used


def dependencies(source, config):
    """Complete identity of the selected seeds without copying them.

    Field framing, file modes and symlink targets match :func:`capture`, so an
    unchanged source yields the same digest on both paths and a prepared cache
    entry can be found before any bytes are written. Nothing here is reusable
    from mtime or size alone.
    """
    source = Path(source).resolve()
    maximum = config["environment"]["max_seed_mb"] * 1024 * 1024
    digest, size = hashlib.sha256(SEED_DIGEST_PREFIX), 0
    for relative in config["environment"]["seed_paths"]:
        size = _seed_identity(digest, _seed_list(_seed_root(source, relative), relative), maximum, size)
    return {"bytes": size, "sha256": digest.hexdigest()}


def capture(source, destination, config):
    """Freeze explicitly selected, untracked dependencies outside candidate write access.

    The digest covers the frozen copy, so a source that changes during the copy
    cannot hand out an identity the frozen bytes do not have.
    """
    source, destination = Path(source).resolve(), Path(destination)
    destination.mkdir(parents=True)
    maximum = config["environment"]["max_seed_mb"] * 1024 * 1024
    size, copied = 0, 0
    digest = hashlib.sha256(SEED_DIGEST_PREFIX)
    def bounded_copy(src, dst):
        nonlocal copied
        if not stat.S_ISREG(os.stat(src, follow_symlinks=False).st_mode):
            raise RuntimeError("Dependency seed must contain regular files")
        with open(src, "rb") as reader, open(dst, "xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                copied += len(chunk)
                if copied > maximum:
                    raise RuntimeError("Dependency snapshot size limit exceeded")
                writer.write(chunk)
        shutil.copystat(src, dst)
        return dst
    for relative in config["environment"]["seed_paths"]:
        path = _seed_root(source, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target, symlinks=True, copy_function=bounded_copy)
        # Hash the frozen copy, with framed fields and filesystem semantics. The
        # copy is what a workspace later receives, so it is the identity.
        scrub_tree(target)
        size = _seed_identity(digest, _seed_list(target, relative), maximum, size)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _cache_frame(path, root):
    info = path.lstat()
    data = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(info.st_mode),
            "type": "link" if path.is_symlink() else "dir" if path.is_dir() else "file"}
    return json.dumps(data, sort_keys=True).encode()


def _cache_digest(root):
    """Integrity digest of a sealed entry; the same frame is computed while copying."""
    digest = hashlib.sha256()
    root = Path(root)
    scrub_tree(root)
    for path in sorted(root.rglob("*")):
        digest.update(_cache_frame(path, root))
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def _seal(tree):
    for path in sorted(Path(tree).rglob("*"), reverse=True) + [Path(tree)]:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def _discard(tree):
    """Remove a tree this module owns, including read-only sealed files."""
    tree = Path(tree)
    if not tree.exists() or tree.is_symlink():
        return
    for path in sorted(tree.rglob("*"), reverse=True) + [tree]:
        if not path.is_symlink():
            try:
                extra = stat.S_IWUSR | stat.S_IXUSR if path.is_dir() else stat.S_IWUSR
                path.chmod(path.stat().st_mode | extra)
            except OSError:
                pass
    shutil.rmtree(tree, ignore_errors=True)


_LOCK_BUSY = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)


def _lock(entry, exclusive=False):
    """Flock the entry directory itself and return its fd, blocking as needed.

    Both sides already share that inode, so no lock file joins the sealed tree:
    the cached listing, the digest and the copy keep exactly their files. flock
    belongs to the open file description, so a second open in this process,
    another thread and another process all contend alike; Darwin and Linux both
    accept it on a directory opened read-only.
    """
    fd = os.open(str(entry), os.O_RDONLY)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError("Prepared environment cache entry is not a directory: " + str(entry))
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _try_exclusive(entry):
    """Take the eviction lock without waiting; None when it is in use or gone."""
    try:
        fd = os.open(str(entry), os.O_RDONLY)
    except FileNotFoundError:
        return None
    held = False
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in _LOCK_BUSY:
                raise
            return None
        held = True
        return fd
    finally:
        if not held:
            os.close(fd)


def _touch(entry):
    """Record recent use for eviction order only, never as an integrity signal.

    Best effort: a read-only cache must still serve a hit, and the shared lock
    protects the copy even when the marker cannot be refreshed.
    """
    try:
        os.utime(entry)
    except OSError:
        pass


def _copy_verified(source, destination):
    """Copy a sealed tree in digest order, hashing each byte once as it is read."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True)
    digest, directories = hashlib.sha256(), []
    for path in sorted(source.rglob("*")):
        info = path.lstat()
        target = destination / path.relative_to(source)
        digest.update(_cache_frame(path, source))
        if path.is_symlink():
            link_target = os.readlink(path)
            digest.update(link_target.encode())
            os.symlink(link_target, target)
        elif path.is_dir():
            target.mkdir()
            directories.append((path, target))
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as reader, target.open("xb") as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(block)
                    writer.write(block)
            shutil.copystat(path, target)
        else:
            raise ValueError("Prepared environment cache contains unsupported entries")
    # Sealed directories are read-only; their modes are applied only once the
    # children exist, otherwise the copy could not be written.
    for path, target in reversed(directories):
        shutil.copystat(path, target)
    shutil.copystat(source, destination)
    return digest.hexdigest()


def materialize(prepared, destination):
    """Copy a sealed entry into a run-owned tree, verifying identity in one pass.

    The cached bytes are read once: the copy both moves and hashes them, and a
    mismatch discards the copy before any of it can reach a workspace. The
    entry stays under a shared lock for the whole copy, so prune skips it
    instead of deleting bytes under an in-flight run; an entry that is already
    gone fails here rather than yielding a silently empty environment.
    """
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Run dependency copy already exists: " + str(destination))
    entry = Path(prepared["path"]).parent
    try:
        fd = _lock(entry)
    except FileNotFoundError:
        raise FileNotFoundError("Prepared environment cache entry is missing: " + str(entry)) from None
    try:
        # The path may have been replaced between lookup and lock; verify the
        # tree we locked is still the tree the manifest named.
        try:
            current, locked = entry.stat(), os.fstat(fd)
        except FileNotFoundError:
            raise FileNotFoundError("Prepared environment cache entry vanished: " + str(entry)) from None
        if (current.st_dev, current.st_ino) != (locked.st_dev, locked.st_ino):
            raise FileNotFoundError("Prepared environment cache entry was replaced: " + str(entry))
        try:
            digest = _copy_verified(prepared["path"], destination)
        except BaseException:
            _discard(destination)
            raise
        if digest != prepared.get("digest"):
            _discard(destination)
            raise ValueError("Prepared environment cache integrity mismatch")
    finally:
        os.close(fd)
    frozen = prepared["inputs"]["seed"]
    return {"bytes": frozen["bytes"], "sha256": frozen["sha256"]}


def _entry(target, key, inputs):
    """Read one sealed entry's manifest under its shared lock, refusing impostors.

    The read and the recent-use marker happen while the lock is held, so an
    eviction either finishes first (and this is a miss) or waits behind it.
    """
    target = Path(target)
    try:
        fd = _lock(target)
    except FileNotFoundError:
        return None
    except (NotADirectoryError, ValueError):
        raise ValueError("Prepared environment cache integrity mismatch") from None
    try:
        try:
            metadata = json.loads((target / "manifest.json").read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise ValueError("Prepared environment cache integrity mismatch") from None
        if metadata.get("key") != key or not isinstance(metadata.get("digest"), str):
            raise ValueError("Prepared environment cache integrity mismatch")
        # Refresh while still holding the shared lock: prune re-reads this time
        # under its exclusive lock, so a hit is never seen as idle mid-return.
        _touch(target)
    finally:
        os.close(fd)
    return {"key": key, "path": str(target / "seeds"), "digest": metadata["digest"],
            "reused": True, "inputs": inputs}


def prune(cache, now=None):
    """Drop the oldest sealed entries beyond the count/byte caps.

    Only completed entries are candidates: abandoned staging trees are removed
    separately, and run-owned copies never live here. Every candidate is locked
    exclusively before it is measured and removed, and the lock is taken without
    waiting, so an entry that a materialize is copying (shared lock) is skipped
    instead of cut short. Modification time is re-read under the lock because a
    hit touches it after this listing; it orders eviction only and never
    authorizes reuse of an entry.
    """
    cache = Path(cache)
    now = time.time() if now is None else now
    entries, staging = [], []
    for path in cache.iterdir():
        # Listing and stats race with concurrent builders and prunes; a name
        # that disappears between the two is simply not a candidate here.
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if path.name.startswith("sealed-"):
            staging.append((mtime, path))
        elif (path / "manifest.json").is_file():
            entries.append((mtime, path))
    entries.sort(key=lambda row: (-row[0], row[1].name))
    removed, total = [], 0
    for index, (mtime, path) in enumerate(entries):
        try:
            size = sum(item.stat().st_size for item in path.rglob("*")
                       if not item.is_symlink() and item.is_file())
        except FileNotFoundError:
            continue
        enough = index < CACHE_ENTRY_LIMIT and total + size <= CACHE_BYTE_LIMIT
        if enough:
            total += size
            continue
        fd = _try_exclusive(path)
        if fd is None:
            # A materialize owns it now, or another prune just removed it.
            if path.exists():
                total += size
            continue
        try:
            try:
                current = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if now - current < CACHE_IDLE_SECONDS:
                total += size
                continue
            _discard(path)
            if not path.exists():
                removed.append(path.name)
        finally:
            os.close(fd)
    for mtime, path in staging:
        if now - mtime >= STAGING_MAX_AGE_SECONDS:
            _discard(path)
    return removed


def prepare_cached(source, cache, config, tools):
    """Cache host-owned dependency seeds by complete inputs; restore always copies.

    A hit runs one copy-free digest of the selected seeds and creates no
    temporary copy; only a miss freezes them. A reused entry is verified while
    it is copied into the run, so no cached byte is used unverified.

    This deliberately does not replay setup commands or network operations. The
    caller must supply an exact tool identity and explicitly selected seed paths.
    """
    from .config import fingerprint
    from .evaluator import MANIFESTS
    source, cache = Path(source).resolve(), Path(cache).resolve()
    if cache == source or source in cache.parents:
        raise ValueError("Prepared cache must be outside the source tree")
    if not tools or not config["environment"]["seed_paths"]:
        raise ValueError("Prepared cache requires exact tools and explicit dependency seeds")
    cache.mkdir(parents=True, exist_ok=True)
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
    def inputs(seed):
        return {"version": 1, "tools": tools, "environment": config["environment"],
                "sandbox": config["sandbox"], "manifests": manifests, "seed": seed}
    scan = inputs(dependencies(source, config))
    key = fingerprint(scan)
    # 命中只做一次只读摘要，不冻结、不建临时副本。
    reused = _entry(cache / key, key, scan)
    if reused:
        return reused
    staging = Path(tempfile.mkdtemp(prefix="sealed-", dir=cache))
    try:
        seed = capture(source, staging / "seeds", config)
        # Freeze first, then key the frozen bytes: a source that changed between
        # the digest and the copy must not authorize the entry the digest named.
        key = fingerprint(inputs(seed))
        target = cache / key
        winner = _entry(target, key, inputs(seed))
        if winner:
            return winner
        _seal(staging / "seeds")
        metadata = {"key": key, "digest": _cache_digest(staging / "seeds"), "inputs": inputs(seed)}
        (staging / "manifest.json").write_text(json.dumps(metadata, sort_keys=True))
        # Rename a completed immutable tree atomically; concurrent identical
        # builders may reuse the winner, which materialize() verifies again.
        try:
            staging.rename(target)
        except OSError:
            if not target.exists():
                raise
            winner = _entry(target, key, inputs(seed))
            if winner is None:
                raise RuntimeError("Prepared environment cache entry vanished during seal") from None
            return winner
        return {"key": key, "path": str(target / "seeds"), "digest": metadata["digest"],
                "reused": False, "inputs": metadata["inputs"], "pruned": prune(cache)}
    finally:
        _discard(staging)


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
