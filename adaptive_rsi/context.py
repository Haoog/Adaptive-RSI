"""Bounded, project-local context selected from tracked paths and evidence."""
import re
from pathlib import Path

from .gitops import git


def terms(text):
    return set(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]{2,}|[\u4e00-\u9fff]{2,8}", text.lower()))


def code_map(workspace, text, limit=24):
    words = terms(text)
    paths = git(workspace, "ls-files").splitlines()
    ranked = []
    for path in paths:
        if path.startswith((".git/", ".rsi-")):
            continue
        score = sum(3 for w in words if w in path.lower())
        ranked.append((-score, path))
    rows = []
    for _, path in sorted(ranked)[:limit]:
        file = Path(workspace) / path
        anchors = []
        if not file.is_symlink() and file.is_file() and file.stat().st_size <= 200000:
            for n, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
                if re.match(r"\s*(?:async\s+)?(?:def |class |function |export |interface |type )", line):
                    anchors.append({"line": n, "declaration": line[:160]})
                if len(anchors) >= 8:
                    break
        rows.append({"path": path, "anchors": anchors})
    return {"files": rows, "total_files": len(paths), "truncated": len(paths) > len(rows)}


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
