import os
import shutil
from pathlib import Path

from .process import ProcessError, checked, scrub_tree


def diff_path(value):
    """Decode Git's C-quoted UTF-8 path without interpreting it as shell input."""
    if value.startswith('"') and value.endswith('"'):
        import codecs
        value = codecs.escape_decode(value[1:-1].encode("utf-8"))[0].decode("utf-8", errors="replace")
    return value[2:] if value.startswith(("a/", "b/")) else value


def git(root, *args, timeout=60):
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0"}
    return checked(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
                    "-C", str(root)] + list(args), env=environment, timeout=timeout)


def repository(path):
    root = Path(path).expanduser().resolve()
    try:
        if not root.is_dir() or git(root, "rev-parse", "--show-toplevel") != str(root):
            raise ValueError("Select the root of an existing Git repository")
        git(root, "rev-parse", "--verify", "HEAD")
    except ProcessError:
        # 目录存在但不是可用仓库根（或还没有提交）时给出固定提示，而不是内部错误。
        raise ValueError("Select the root of an existing Git repository") from None
    return root


def snapshot(source, destination, sha=None):
    destination = Path(destination)
    commit = git(source, "rev-parse", "--verify", (sha or "HEAD") + "^{commit}")
    checked(["git", "-c", "core.hooksPath=" + os.devnull, "init", "--quiet", str(destination)])
    git(destination, "remote", "add", "origin", str(source))
    # A depth-one fetch of this exact SHA excludes later fixes, tags and unrelated refs.
    checked(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=always",
             "-C", str(destination), "fetch", "--quiet", "--depth=1", "--no-tags", "origin", commit], timeout=180)
    git(destination, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    scrub_tree(destination)
    exclude_file = destination / ".git" / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    with exclude_file.open("a") as stream:
        stream.write("\n.rsi-agent/\n.rsi-home/\n.rsi-build/\n.rsi-tmp/\n")
    return destination


def changes(workspace):
    git(workspace, "add", "-A")
    raw = git(workspace, "diff", "--cached", "--name-only", "-z")
    files = [f for f in raw.split("\0") if f]
    patch = git(workspace, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")
    return files, patch


def copy_source(source, destination):
    scrub_tree(source)
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".rsi-agent", ".rsi-home", ".rsi-build", "__pycache__"))
    return Path(destination)


def origin_repository(root):
    try:
        remote = git(root, "remote", "get-url", "origin")
        import re
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
        return match.group(1) if match else ""
    except RuntimeError:
        return ""


def github_url(value):
    import re
    if not isinstance(value, str):
        raise ValueError("Expected a GitHub repository URL")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", value.strip())
    if not match or any(part in [".", ".."] for part in match.group(1).split("/")):
        raise ValueError("Use a GitHub repository URL such as https://github.com/owner/repository")
    return match.group(1), "https://github.com/" + match.group(1) + ".git"
