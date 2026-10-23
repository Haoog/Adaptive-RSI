"""Bounded retry evidence and persistent workflow decisions."""
import json
import re
import time
from pathlib import PurePosixPath


def safe_excerpt(value, limit=1200):
    text = str(value or "")
    text = re.sub(r"https?://\S+", "<URL redacted>", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted>", text)
    return text[:limit]


def retry_feedback(row, protected=()):
    result = json.loads(row["result"])
    feedback = {key: row.get(key) for key in ["id", "base", "state"]}
    variants = {variant for value in protected for variant in
                (value, json.dumps(value, ensure_ascii=True)[1:-1], repr(value)[1:-1]) if variant}
    def excerpt(value, limit=700):
        # Bound the matcher by the output size, never by the size of sealed fixtures.
        text = str(value or "")[:limit + 24]
        windows = {text[i:i + 24] for i in range(len(text) - 23)}
        pattern = re.compile("|".join(re.escape(part) for part in windows)) if windows else None
        for fragment in sorted(variants, key=len, reverse=True):
            if len(fragment) >= 24:
                if pattern and pattern.search(fragment):
                    return "<sealed>"
            elif fragment:
                text = re.sub(r"(?<!\w)" + re.escape(fragment) + r"(?!\w)", "<sealed>", text)
        return safe_excerpt(text, limit)
    for key in ["summary", "patch_hash"]:
        if key in result:
            feedback[key] = excerpt(result[key])
    if result.get("failure"):
        feedback["failure"] = {k: excerpt(result["failure"].get(k)) for k in ["code", "action"]}
    feedback["review_findings"] = [{k: excerpt(finding.get(k)) for k in
                                  ["severity", "path", "line", "title", "reason", "suggestion"]}
                                 for finding in result.get("review", {}).get("findings", [])[:8]
                                 if ".rsi-acceptance" not in PurePosixPath(str(finding.get("path", ""))).parts
                                 and not any(".rsi-acceptance" in str(v) for v in finding.values())]
    # Sealed acceptance files, commands, logs and protected samples never enter retry prompts.
    checks = result.get("evaluation", {}).get("checks", {})
    feedback["failed_regression_checks"] = [{"stage": stage, "name": excerpt(c.get("name"), 200),
                                             "exit_code": c.get("exit_code"),
                                             "stderr": excerpt(c.get("stderr"), 1000)}
                                            for stage in ["candidate", "candidate_full"]
                                            for c in checks.get(stage, []) if not c.get("passed")][:5]
    return feedback


def protected_feedback_values(store, task):
    """Remove literal sealed samples even when quoted in application-file findings."""
    import ast
    values = set()
    rows = store.query("SELECT s.data FROM stages s JOIN runs r ON r.id=s.run WHERE r.task=? AND s.stage='acceptance' ORDER BY s.created DESC LIMIT 2", (task,))
    for row in rows:
        for item in json.loads(row["data"]).get("files", []):
            content = item.get("content", "")
            values.update(line.strip() for line in content.splitlines() if len(line.strip()) >= 10)
            try:
                for node in ast.walk(ast.parse(content)):
                    if isinstance(node, ast.Constant) and type(node.value) in [str, int, float]:
                        values.add(str(node.value))
            except (SyntaxError, ValueError):
                values.update(re.findall(r"[\"']([^\"'\n]+)[\"']", content))
            # Acceptance may use JavaScript, shell or other languages, not only Python.
            for number in re.findall(r"(?<![\w.])(?:0[xX][\da-fA-F_]+|[+-]?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)(?!\w)", content):
                values.add(number)
                try:
                    values.add(str(int(number.replace('_', ''), 16)) if number.lower().startswith('0x') else format(float(number.replace('_', '')), '.15g'))
                except (ValueError, OverflowError):
                    pass
    return sorted((v for v in values if v), key=len, reverse=True)


def queue_status(store, project):
    now = time.time()
    paused = store.one("SELECT paused FROM projects WHERE id=?", (project,))
    busy = store.one("SELECT id FROM runs WHERE project=? AND ended IS NULL", (project,))
    rows = store.query("SELECT * FROM tasks WHERE project=? AND state='queued' ORDER BY " + store.queue_order(), (project, now))
    ready = 0
    for row in rows:
        dependency = store.one("SELECT p.id,p.state,p.title FROM task_dependencies d JOIN tasks p ON p.id=d.dependency WHERE d.task=?", (row["id"],))
        reason = "dependency" if dependency and dependency["state"] != "merged" else "backoff" if row["available"] > now else "paused" if paused and paused["paused"] else "running" if busy else "ready"
        if reason in ["running", "ready"]:
            ready += 1
        row.update(wait_reason=reason, queue_position=ready if reason in ["running", "ready"] else None,
                   dependency=dependency)
    return rows
