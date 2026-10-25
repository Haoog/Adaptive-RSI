import hashlib
import os
import shutil
from pathlib import Path

from .gitops import git
from .process import scrub_tree


def capture(source, destination, config):
    """Freeze explicitly selected, untracked dependencies outside candidate write access."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True)
    maximum = config["environment"]["max_seed_mb"] * 1024 * 1024
    size, digest = 0, hashlib.sha256()
    for relative in config["environment"]["seed_paths"]:
        path = (source / relative).resolve()
        path.relative_to(source)
        if not path.is_dir():
            raise RuntimeError("Dependency seed directory missing: " + relative)
        if git(source, "ls-files", "--", relative):
            raise RuntimeError("Seed only untracked dependencies; tracked files already enter the Git snapshot: " + relative)
        scrub_tree(path)
        for parent, dirs, files in os.walk(path, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                item = Path(parent) / name
                size += item.stat().st_size
                if size > maximum:
                    raise RuntimeError("Dependency snapshot size limit exceeded")
                digest.update(str(item.relative_to(source)).encode())
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(path, target, symlinks=True)
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
