"""Bounded, project-local context selected from tracked paths and evidence."""
import re
from pathlib import Path

from .gitops import git


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


def code_map(workspace, text, limit=24):
    words = terms(text)
    paths = git(workspace, "ls-files").splitlines()
    ranked = []
    content_budget = 12 * 1024 * 1024
    text_suffixes = {
        ".c", ".cc", ".cpp", ".cs", ".css", ".dart", ".ex", ".exs", ".fs", ".go",
        ".h", ".hpp", ".html", ".java", ".js", ".jsx", ".json", ".kt", ".kts",
        ".lua", ".m", ".md", ".php", ".pl", ".py", ".rb", ".rs", ".scss", ".sh",
        ".sql", ".swift", ".toml", ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
    }
    path_words = sorted(words, key=lambda word: (-len(word), word))[:256]
    content_words = path_words[:128]
    for path in paths:
        if path.startswith((".git/", ".rsi-")):
            continue
        lower_path = path.lower()
        score = sum(6 for word in path_words if word in lower_path)
        if any(word == Path(path).stem.lower() for word in path_words):
            score += 12
        content = ""
        file = Path(workspace) / path
        if file.suffix.lower() in text_suffixes and not file.is_symlink() and file.is_file():
            try:
                size = file.stat().st_size
                if size <= 160000 and size <= content_budget:
                    content = file.read_text(errors="replace").lower()
                    content_budget -= size
                    score += sum(1 for word in content_words if word in content)
            except OSError:
                content = ""
        ranked.append((-score, path))
    rows = []
    for negative_score, path in sorted(ranked)[:limit]:
        file = Path(workspace) / path
        anchors = []
        if not file.is_symlink() and file.is_file() and file.stat().st_size <= 200000:
            for n, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
                if re.match(r"\s*(?:async\s+)?(?:def |class |function |export |interface |type )", line):
                    anchors.append({"line": n, "declaration": line[:160]})
                if len(anchors) >= 8:
                    break
            if -negative_score:
                lowered = file.read_text(errors="replace").lower().splitlines()
                present = {item["line"] for item in anchors}
                for n, line in enumerate(lowered, 1):
                    if n not in present and any(word in line for word in content_words):
                        anchors.append({"line": n, "declaration": line[:160]})
                        if len(anchors) >= 12:
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
