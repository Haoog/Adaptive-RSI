"""Bounded, project-local context selected from tracked paths and evidence."""
import copy
import hashlib
import os
import re
import time
from functools import lru_cache
from pathlib import Path

from .gitops import git


_INDEX_BYTES = 12 * 1024 * 1024
_FILE_BYTES = 1024 * 1024


@lru_cache(maxsize=512)
def terms(text):
    """Return searchable ASCII words and overlapping CJK fragments.

    Chinese does not use spaces, so treating an entire phrase as one term made
    goals such as ``修复登录失败`` miss code containing only ``登录``. Bigrams
    and trigrams provide a bounded, dependency-free index without guessing word
    boundaries.
    """
    value = text.lower()
    result = set(re.findall(r"[a-z_][a-z0-9_]{1,}", value))
    cjk_terms = set()
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        if len(run) <= 8:
            cjk_terms.add(run)
        for size in (2, 3):
            cjk_terms.update(run[index:index + size] for index in range(max(0, len(run) - size + 1)))
            if len(cjk_terms) >= 256:
                break
        if len(cjk_terms) >= 256:
            break
    result.update(sorted(cjk_terms)[:256])
    return {term for term in sorted(result)[:512] if len(term) >= 2}


def _workspace_fingerprint(workspace):
    """Hash the checked-out base plus any local content that can affect retrieval."""
    root = Path(workspace).resolve()
    try:
        head = git(root, "rev-parse", "HEAD")
        changed = [name for name in git(
            root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "HEAD", "--").split("\0") if name]
        untracked = [name for name in git(
            root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name]
        names = sorted(set(changed) | set(untracked))
    except RuntimeError:
        head = ""
        names = []
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git" and not name.startswith(".rsi-")]
            names.extend((Path(parent) / name).relative_to(root).as_posix() for name in files
                         if not name.startswith(".rsi-"))

    digest = hashlib.sha256(head.encode("utf-8"))
    remaining = _INDEX_BYTES
    for name in names:
        path = root / name
        digest.update(name.encode("utf-8", "surrogateescape"))
        if path.is_symlink():
            digest.update(b"symlink\0" + os.readlink(path).encode("utf-8", "surrogateescape"))
            continue
        if not path.is_file():
            digest.update(b"missing\0")
            continue
        size = path.stat().st_size
        if size > remaining:
            # Never let an unusually large dirty tree make an unsafe cache hit.
            return head + ":" + str(time.time_ns())
        remaining -= size
        with path.open("rb") as stream:
            unread = size
            while unread:
                chunk = stream.read(min(_FILE_BYTES, unread))
                if not chunk:
                    return head + ":" + str(time.time_ns())
                unread -= len(chunk)
                digest.update(chunk)
            if os.fstat(stream.fileno()).st_size != size:
                return head + ":" + str(time.time_ns())
    return digest.hexdigest()


_DECLARATION = re.compile(
    r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?"
    r"(?:def|class|function|interface|type|fn|struct|trait|enum|const|let|var)[ \t]+"
    r"(?P<symbol>[^\W\d]\w*)[^\r\n]*", re.MULTILINE,
)


@lru_cache(maxsize=3)
def _code_index(root, fingerprint, cited_paths=()):
    """Cache the bounded text index for one exact workspace content state."""
    workspace = Path(root)
    cited = set(cited_paths)
    paths = sorted(set(filter(None, git(workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0"))),
                   key=lambda path: (path not in cited, path))
    rows = []
    content_budget = _INDEX_BYTES
    text_suffixes = {
        ".c", ".cc", ".cjs", ".cpp", ".cs", ".css", ".dart", ".ex", ".exs", ".fs", ".go",
        ".h", ".hpp", ".html", ".java", ".js", ".jsx", ".json", ".kt", ".kts",
        ".lua", ".m", ".md", ".mjs", ".php", ".pl", ".py", ".pyi", ".rb", ".rs", ".scss", ".sh",
        ".sql", ".swift", ".toml", ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
    }
    for path in paths:
        if path.startswith((".git/", ".rsi-")):
            continue
        content = ""
        file = workspace / path
        if content_budget and file.suffix.lower() in text_suffixes:
            try:
                if file.resolve() == file and file.is_file():
                    size = file.stat().st_size
                    with file.open("rb") as stream:
                        data = stream.read(min(_FILE_BYTES, content_budget))
                    content_budget -= len(data)
                    if len(data) < size:
                        # Keep complete lines so every anchor has its original line number and text.
                        data = data[:data.rfind(b"\n") + 1]
                    if b"\0" not in data:
                        content = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
            except (OSError, RuntimeError):
                pass
        declarations = []
        line, offset = 1, 0
        for match in _DECLARATION.finditer(content):
            line += content.count("\n", offset, match.start())
            offset = match.start()
            declarations.append((line, match["symbol"].lower(), content[offset: min(match.end(), offset + 160)]))
        rows.append((path, content, declarations))
    return tuple(rows), len(paths)


@lru_cache(maxsize=64)
def _code_map_cached(root, fingerprint, text, limit, evidence=()):
    quotes = {}
    for path, quote in evidence:
        quotes.setdefault(path, []).append(quote)
    indexed, total_files = _code_index(root, fingerprint, tuple(sorted(quotes)))
    path_words = sorted(terms(text), key=lambda word: (-len(word), word))[:256]
    content_words = path_words[:128]
    symbol_words = set(content_words)
    ranked = []
    for path, content, declarations in indexed:
        lower_path = path.lower()
        score = sum(6 for word in path_words if word in lower_path)
        if Path(path).stem.lower() in path_words:
            score += 12
        if content:
            lowered = content.lower()
            score += sum(1 for word in content_words if word in lowered)
        matches = symbol_words.intersection(symbol for _, symbol, _ in declarations)
        score += 16 * min(len(matches), 3)
        ranked.append((path not in quotes, -score, path, content, declarations, matches))
    rows = []
    for _, negative_score, path, content, declarations, matches in sorted(ranked)[:limit]:
        anchors = {}
        for quote in quotes.get(path, []):
            offset = content.find(quote) if quote else -1
            if offset >= 0:
                start = content.rfind("\n", 0, offset) + 1
                line = content.count("\n", 0, start) + 1
                anchors[line] = content[start:start + 160].split("\n", 1)[0]
        for line, symbol, declaration in declarations:
            if len(anchors) >= 8:
                break
            if symbol in matches:
                anchors.setdefault(line, declaration)
        for line, _, declaration in declarations:
            if len(anchors) >= 8:
                break
            anchors.setdefault(line, declaration)
        if negative_score:
            for line, value in enumerate(content.splitlines(), 1):
                if len(anchors) >= 12:
                    break
                lowered = value.lower()
                if line not in anchors and any(word in lowered for word in content_words):
                    anchors[line] = value[:160]
        rows.append({"path": path, "anchors": [{"line": line, "declaration": value} for line, value in anchors.items()]})
    return {"files": rows, "total_files": total_files, "truncated": total_files > len(rows)}


def code_map(workspace, text, limit=24, *, evidence=()):
    root = str(Path(workspace).resolve())
    fingerprint = _workspace_fingerprint(root)
    return copy.deepcopy(_code_map_cached(root, fingerprint, text, limit, tuple(evidence)[:8]))


def related(rows, text, limit=3, characters=5000):
    words = terms(text)
    def matches(row):
        return words & terms(row.get("content", "") + " " + " ".join(row.get("triggers", [])))
    ranked = sorted(rows, key=lambda r: (-len(matches(r)), -r.get("created", 0)))
    result, used = [], 0
    for row in ranked:
        content = row.get("content", "")
        if not matches(row) or used + len(content) > characters:
            continue
        result.append(row)
        used += len(content)
        if len(result) == limit:
            break
    return result
