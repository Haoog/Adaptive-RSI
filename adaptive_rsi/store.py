import contextlib
import contextvars
from collections import deque
import hashlib
import json
import math
import queue
import re
import sqlite3
import statistics
import threading
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError


# /api/state 单次请求会执行十几条查询；8 个复用连接足以覆盖线程池的并发峰值，
# 池满时新借出的连接用完直接关闭，不阻塞调用方。
POOL_SIZE = 8
MAX_CONNECTIONS = 32


def uid():
    return uuid.uuid4().hex[:16]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_object(value):
    """Isolate malformed historical read-model evidence without rewriting it."""
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        result = None
    return result if isinstance(result, dict) else {"evidence_error": "invalid_json_object"}


EVIDENCE_OBJECTS = ("review", "evaluation", "feedback", "failure", "automation", "value", "monitor")


def decode_evidence(value):
    """Validate containers consumed by historical dashboards, preserving raw database evidence.

    只丢弃无法解释的字段并记录 evidence_error；其余顶层证据（summary、files、reason、
    merged_sha 等）保留，避免一个坏字段让整份结果从读模型消失。历史生产者用空列表
    表示“没有可执行检查”，它等价于空对象；非空列表仍视为无效证据。
    """
    result = decode_object(value)
    if "evidence_error" in result:
        return result
    errors = []
    for field in EVIDENCE_OBJECTS:
        if field in result and not isinstance(result[field], dict):
            errors.append(field)
            result.pop(field)
    review, evaluation = result.get("review"), result.get("evaluation")
    review = review if isinstance(review, dict) else {}
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    findings, trials = review.get("findings", []), result.get("trials", [])
    if not isinstance(findings, list) or any(not isinstance(row, dict) for row in findings):
        errors.append("findings")
        result.pop("review", None)
    if not isinstance(trials, list) or any(not isinstance(row, dict) for row in trials):
        errors.append("trials")
        result.pop("trials", None)
    elif any("case" in row and not isinstance(row["case"], (str, type(None))) for row in trials):
        errors.append("case")
        result.pop("trials", None)
    checks = evaluation.get("checks", {})
    empty_checks = isinstance(checks, list) and not checks
    if empty_checks:
        evaluation["checks"], checks = {}, {}
    if not isinstance(checks, dict) or any(not isinstance(rows, list) or
            any(not isinstance(row, dict) for row in rows) for rows in checks.values()):
        errors.append("checks")
        result.pop("evaluation", None)
    elif empty_checks and evaluation.get("decision") == "pass":
        # 空列表兼容只表示“没有可执行检查”，不构成验收证据；沿用 pass 会让
        # 0 个检查的记录在质量/工作流读模型里被计成 passed/accepted。
        evaluation["decision"] = "insufficient_evidence"
    if "merged_at" in result and (not isinstance(result["merged_at"], (int, float)) or
            not (0 <= result["merged_at"] < 2**63 - 1)):
        errors.append("merged_at")
        result.pop("merged_at")
    if errors:
        result["evidence_error"] = "invalid_evidence_shape:" + errors[0]
    return result


def provider_identity(provider, generation=""):
    """Stable provider identity used by the catalog and by call price freezing."""
    from .config import fingerprint
    fields = {key: value for key, value in provider.items() if key != "name"}
    if generation:
        fields["credential_generation"] = generation
    return fingerprint(fields)


def credential_generation(root, provider):
    """Persisted generation for this provider's own credential; never returns a secret."""
    try:
        from .credentials import Credentials
        generation = Credentials(root).epoch(provider)
    except Exception:
        # 价格解析失败不能阻断模型调用；退回不含代次的历史身份而不是猜测。
        return ""
    return generation if isinstance(generation, str) else ""


def catalog_keys(project, provider_id, signature, legacy_signature):
    """Settings keys that may hold a provider catalog, most specific first.

    签名含凭据代次；不含代次的历史目录只作为最后回退。项目范围只在同一签名下
    继承全局目录，且不继承不含连接身份的价格偏好，避免把另一连接的价格套过来。
    """
    scopes = [project, "global"] if project and project != "global" else ["global"]
    keys, seen = [], set()
    for index, scope in enumerate(scopes):
        base = "model-center:" + scope + ":" + provider_id + ":"
        candidates = [(base + signature, False)]
        if index == 0:
            candidates.append(("model-preferences:" + scope + ":" + provider_id, False))
        if legacy_signature and legacy_signature != signature:
            candidates.append((base + legacy_signature, True))
        for key, legacy in candidates:
            if key not in seen:
                seen.add(key)
                keys.append((key, legacy))
    return keys


class PooledConnection(sqlite3.Connection):
    """携带创建时数据库文件标识的连接，用于识别被整体替换的数据库。"""

    identity = None


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "state.sqlite3"
        # 有界连接池；__init__ 的 DDL 迁移同样经由 connect() 走借出-归还路径。
        self._pool = queue.LifoQueue(maxsize=POOL_SIZE)
        self._pool_lock = threading.Lock()
        self._pool_generation = 0
        self._clock_samples = {}
        self._connection_capacity = threading.BoundedSemaphore(MAX_CONNECTIONS + POOL_SIZE)
        self._metrics_lock = threading.Lock()
        self._metrics = {"opened": 0, "borrowed": 0, "waiting": 0, "peak": 0}
        self._read_snapshot = threading.local()
        self._query_samples = deque(maxlen=128)
        self._wait_samples = deque(maxlen=512)
        self._slow_queries = deque(maxlen=20)
        self._query_context = contextvars.ContextVar("rsi_query_context", default={})
        self._active_snapshots = {}
        self._poll_log_windows = {}
        self._request_log_counts = {"observed": 0, "persisted": 0, "sampled_out": 0, "failed": 0}
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("Database schema is newer than this runtime")
            from .schema_migrations import apply, heal, validate_history
            # 先补账本再校验：已生效的迁移只回填账本行，不重放；无法确认时 heal 直接报错。
            heal(db)
            sealed = validate_history(db)
            if not sealed:
                self._bootstrap(db)
            apply(db)

    def _bootstrap(self, db):
        from .schema_migrations import BOOTSTRAP_SCHEMA
        db.executescript(BOOTSTRAP_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        for field, definition in [("urgency", "INTEGER DEFAULT 0"), ("infra_retries", "INTEGER DEFAULT 0"),
                                  ("budget_waits", "INTEGER DEFAULT 0")]:
            if field not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN " + field + " " + definition)
        if "provider" not in {row["name"] for row in db.execute("PRAGMA table_info(calls)")}:
            db.execute("ALTER TABLE calls ADD COLUMN provider TEXT DEFAULT 'openai'")
        if "unit_price" not in {row["name"] for row in db.execute("PRAGMA table_info(calls)")}:
            db.execute("ALTER TABLE calls ADD COLUMN unit_price REAL")
        if "unit" not in {row["name"] for row in db.execute("PRAGMA table_info(calls)")}:
            db.execute("ALTER TABLE calls ADD COLUMN unit TEXT DEFAULT 'unknown'")
        for field in ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "usage_known"]:
            if field not in {row["name"] for row in db.execute("PRAGMA table_info(calls)")}:
                db.execute("ALTER TABLE calls ADD COLUMN " + field + " INTEGER DEFAULT 0")
        db.execute("CREATE INDEX IF NOT EXISTS run_calls ON calls(run,created)")
        db.execute("CREATE INDEX IF NOT EXISTS created_calls ON calls(created)")
        for table in ("phase_times", "remote_waits"):
            if "duration" not in {row["name"] for row in db.execute("PRAGMA table_info(" + table + ")")}:
                db.execute("ALTER TABLE " + table + " ADD COLUMN duration REAL")
        # 只撤销旧版本遗留授权一次；之后保留用户明确保存的项目策略。
        migration = db.execute("SELECT 1 FROM settings WHERE key='delivery-policy-v2'").fetchone()
        if not migration:
            for row in db.execute("SELECT id,config FROM projects").fetchall():
                config = json.loads(row["config"])
                if "delivery" in config:
                    config["delivery"].update(mode="local", auto_merge=False)
                    db.execute("UPDATE projects SET config=? WHERE id=?", (encode(config), row["id"]))
            row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
            if row:
                config = json.loads(row["value"])
                if "delivery" in config:
                    config["delivery"].update(mode="local", auto_merge=False)
                    db.execute("UPDATE settings SET value=? WHERE key='config'", (encode(config),))
            db.execute("INSERT INTO settings VALUES ('delivery-policy-v2','true')")
        db.execute("DELETE FROM schedules WHERE kind='deep'")
        db.execute("UPDATE tasks SET state='obsolete' WHERE source='system_deep' AND state IN ('queued','waiting_budget')")
        db.execute("INSERT OR IGNORE INTO task_completions SELECT id,project,updated FROM tasks "
                   "WHERE source NOT LIKE 'system_%' AND state IN ('awaiting_approval','pr_open','merged','completed')")
        checksum = self._migration_checksum(1)
        migration = db.execute("SELECT checksum FROM schema_migrations WHERE version=1").fetchone()
        if migration and migration["checksum"] != checksum:
            raise RuntimeError("Database migration checksum mismatch")
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1,?,?)", (checksum, time.time()))
        # Version 2 adds read-only observability tables and indexes.
        db.execute("DROP INDEX IF EXISTS request_log_created")
        checksum = self._migration_checksum(2)
        migration = db.execute("SELECT checksum FROM schema_migrations WHERE version=2").fetchone()
        if migration and migration["checksum"] != checksum:
            raise RuntimeError("Database migration checksum mismatch")
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2,?,?)", (checksum, time.time()))
        db.execute("PRAGMA user_version=2")

    @staticmethod
    def _migration_checksum(version):
        import hashlib
        return hashlib.sha256(("adaptive-rsi-schema-v" + str(version)).encode()).hexdigest()

    def _file_identity(self):
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _new_connection(self):
        # WAL 持久保存在数据库文件中，busy timeout 由 connect(timeout=15) 设置；
        # 借出期间连接由单线程独占，check_same_thread=False 因此是安全的。
        db = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False, factory=PooledConnection)
        try:
            db.identity = self._file_identity()
            db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _prepare(db):
        # 调用方可能在持有期间改过连接级设置，每次借出重新确保生效（PRAGMA 幂等）。
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextlib.contextmanager
    def connect(self):
        waiting_since = time.monotonic()
        with self._metrics_lock:
            self._metrics["waiting"] += 1
        if not self._connection_capacity.acquire(timeout=15):
            with self._metrics_lock:
                self._metrics["waiting"] -= 1
                self._metrics["timeouts"] = self._metrics.get("timeouts", 0) + 1
                waited = time.monotonic() - waiting_since
                self._metrics["wait_seconds"] = self._metrics.get("wait_seconds", 0) + waited
                self._wait_samples.append(waited)
            raise sqlite3.OperationalError("Database connection capacity exhausted")
        with self._metrics_lock:
            self._metrics["waiting"] -= 1
            self._metrics["borrowed"] += 1
            self._metrics["peak"] = max(self._metrics["peak"], self._metrics["borrowed"])
            waited = time.monotonic() - waiting_since
            self._metrics["wait_seconds"] = self._metrics.get("wait_seconds", 0) + waited
            self._wait_samples.append(waited)
        db = None
        try:
            with self._pool_lock:
                generation = self._pool_generation
                try:
                    db = self._pool.get_nowait()
                except queue.Empty:
                    pass
            if db is not None and db.identity != self._file_identity():
                db.close()
                db = None
            if db is None:
                db = self._new_connection()
                with self._metrics_lock:
                    self._metrics["opened"] += 1
            try:
                self._prepare(db)
            except sqlite3.Error:
                db.close()
                db = None
                db = self._new_connection()
                self._prepare(db)
            yield db
            db.commit()
            with self._pool_lock:
                if generation == self._pool_generation:
                    try:
                        self._pool.put_nowait(db)
                        db = None
                    except queue.Full:
                        pass
        finally:
            try:
                if db is not None:
                    try:
                        db.rollback()
                    except sqlite3.Error:
                        pass
                    try:
                        db.close()
                    except sqlite3.Error:
                        with self._metrics_lock:
                            self._metrics["close_failures"] = self._metrics.get("close_failures", 0) + 1
            finally:
                with self._metrics_lock:
                    self._metrics["borrowed"] -= 1
                self._connection_capacity.release()

    def close(self):
        """关闭池中空闲连接；已借出的连接由各自的 with 块负责关闭。"""
        with self._pool_lock:
            self._pool_generation += 1
            while True:
                try:
                    conn = self._pool.get_nowait()
                except queue.Empty:
                    return
                try:
                    conn.close()
                except Exception:
                    pass

    @contextlib.contextmanager
    def query_scope(self, **identity):
        context = dict(self._query_context.get())
        context.update({key: value for key, value in identity.items() if key in ("request", "run", "task")
                        and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)})
        token = self._query_context.set(context)
        try:
            yield
        finally:
            self._query_context.reset(token)

    def query(self, sql, args=()):
        started = time.monotonic()
        execution_seconds = None
        def execute(db):
            nonlocal execution_seconds
            began = time.monotonic()
            try:
                return [dict(row) for row in db.execute(sql, args).fetchall()]
            finally:
                execution_seconds = time.monotonic() - began
        try:
            active = getattr(self._read_snapshot, "connection", None)
            if active is not None:
                return execute(active)
            with self.connect() as db:
                return execute(db)
        finally:
            elapsed = time.monotonic() - started
            with self._metrics_lock:
                self._metrics["queries"] = self._metrics.get("queries", 0) + 1
                self._query_samples.append(elapsed)
                if execution_seconds is not None and execution_seconds > 0.1:
                    normalized = re.sub(r"'(?:''|[^'])*'", "?", " ".join(sql.split()))
                    self._slow_queries.append({"fingerprint": hashlib.sha256(normalized.encode()).hexdigest()[:16],
                                               "seconds": execution_seconds, "total_seconds": elapsed, "at": time.time(),
                                               "context": dict(self._query_context.get())})

    def stream(self, sql, args=()):
        """Yield rows without materializing an unbounded result set."""
        active = getattr(self._read_snapshot, "connection", None)
        if active is not None:
            for row in active.execute(sql, args):
                yield dict(row)
            return
        with self.connect() as db:
            for row in db.execute(sql, args):
                yield dict(row)

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        active = getattr(self._read_snapshot, "connection", None)
        if active is not None:
            active.execute(sql, args)
            return
        with self.connect() as db:
            db.execute(sql, args)

    @contextlib.contextmanager
    def snapshot(self):
        """Serve related reads from one SQLite transaction on this thread."""
        if getattr(self._read_snapshot, "connection", None) is not None:
            yield
            return
        with self.connect() as db:
            db.execute("BEGIN")
            self._read_snapshot.connection = db
            token = uuid.uuid4().hex
            with self._metrics_lock:
                self._active_snapshots[token] = time.monotonic()
            try:
                yield
            finally:
                with self._metrics_lock:
                    self._active_snapshots.pop(token, None)
                self._read_snapshot.connection = None

    def wal_diagnostics(self, checkpoint=False):
        """A passive checkpoint never waits for readers or truncates their WAL."""
        now = time.monotonic()
        with self._metrics_lock:
            ages = [max(0, now - started) for started in self._active_snapshots.values()]
        result = {"active_snapshots": len(ages), "oldest_snapshot_seconds": max(ages, default=0),
                  "snapshot_scope": "this_process", "checked_at": time.time()}
        if checkpoint:
            with self.connect() as db:
                busy, pages, copied = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                result.update(busy=bool(busy), wal_pages=pages, checkpointed_pages=copied,
                              pending_pages=max(0, pages - copied), mode="passive")
            self._wal_snapshot = result
        else:
            previous = getattr(self, "_wal_snapshot", {})
            result.update({key: value for key, value in previous.items()
                           if key not in ("active_snapshots", "oldest_snapshot_seconds", "checked_at", "snapshot_scope")})
            result["checkpoint_checked_at"] = previous.get("checked_at")
        return result

    def record_request(self, request, method, path, status, started, ended, error_code=""):
        polls = {"/api/state", "/api/events", "/api/usage", "/api/auth/status", "/api/operation"}
        sampled = method == "GET" and path in polls and 200 <= status < 400
        window = int(time.monotonic() // 60)
        with self._metrics_lock:
            self._request_log_counts["observed"] += 1
            if sampled:
                if self._poll_log_windows.get(path) == window:
                    self._request_log_counts["sampled_out"] += 1
                    return False
                self._poll_log_windows[path] = window
        try:
            self.execute("INSERT INTO request_log VALUES (?,?,?,?,?,?,?)",
                         (request, method, path[:300], status, started, ended, error_code[:100]))
        except Exception:
            with self._metrics_lock:
                self._request_log_counts["failed"] += 1
                if sampled and self._poll_log_windows.get(path) == window:
                    self._poll_log_windows.pop(path, None)
            raise
        with self._metrics_lock:
            self._request_log_counts["persisted"] += 1
        return True

    def metrics(self, integrity=False):
        with self._metrics_lock:
            borrowed = self._metrics["borrowed"]
            values = dict(self._metrics)
            waits = sorted(self._wait_samples)
            quantile = lambda fraction: waits[max(0, math.ceil(len(waits) * fraction) - 1)] if waits else None
            values.update(wait_p50_seconds=quantile(.5), wait_p95_seconds=quantile(.95), wait_p99_seconds=quantile(.99),
                          wait_samples=len(waits), wait_window=512, wait_quantile="nearest_rank", wait_timeout_seconds=15,
                          wait_scope="completed_admissions_including_timeouts_this_process",
                          slow_queries=list(self._slow_queries))
            values["request_logging"] = {**self._request_log_counts, "poll_sample_seconds": 60,
                                         "counter_scope": "this_process", "errors_and_writes": "all"}
        with self.connect() as db:
            values.update({
                "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "wal_bytes": Path(str(self.path) + "-wal").stat().st_size
                if Path(str(self.path) + "-wal").exists() else 0,
                "page_count": db.execute("PRAGMA page_count").fetchone()[0],
                "page_size": db.execute("PRAGMA page_size").fetchone()[0],
                "busy_timeout_ms": db.execute("PRAGMA busy_timeout").fetchone()[0],
            })
            if integrity:
                deadline = time.monotonic() + 2
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                try:
                    rows = db.execute("PRAGMA foreign_key_check").fetchmany(101)
                    self._integrity_snapshot = {"foreign_key_violations": len(rows),
                        "foreign_key_truncated": len(rows) > 100, "foreign_key_checked_at": time.time()}
                except sqlite3.OperationalError:
                    self._integrity_snapshot = {"foreign_key_violations": None,
                        "foreign_key_check": "budget_exceeded", "foreign_key_checked_at": time.time()}
                finally:
                    db.set_progress_handler(None, 0)
        values.update(getattr(self, "_integrity_snapshot", {"foreign_key_violations": None,
                                                          "foreign_key_check": "not_requested"}))
        values.update(idle=self._pool.qsize(), borrowed=borrowed, limit=MAX_CONNECTIONS + POOL_SIZE)
        return values

    def task_duration_model(self):
        rows = self.query("SELECT max(0,ended-started) AS seconds FROM runs "
                          "WHERE ended IS NOT NULL AND started IS NOT NULL ORDER BY ended DESC LIMIT 128")
        samples = [row["seconds"] for row in rows]
        return {"samples": len(samples), "median_seconds": statistics.median(samples) if samples else 0.0,
                "source": "completed_runs", "window": 128}

    def schema_migrations(self):
        return self.query("SELECT version,checksum,applied FROM schema_migrations ORDER BY version")

    def relationship_audit(self, limit=100):
        checks = {
            "tasks_without_project": "SELECT t.id FROM tasks t LEFT JOIN projects p ON p.id=t.project WHERE p.id IS NULL",
            "runs_without_project": "SELECT r.id FROM runs r LEFT JOIN projects p ON p.id=r.project WHERE p.id IS NULL",
            "run_project_mismatch": "SELECT r.id FROM runs r JOIN tasks t ON t.id=r.task WHERE r.project IS NOT t.project",
            "runs_without_task": "SELECT r.id FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.task IS NOT NULL AND t.id IS NULL",
            "events_without_project": "SELECT e.seq FROM events e LEFT JOIN projects p ON p.id=e.project WHERE e.project IS NOT NULL AND p.id IS NULL AND NOT (e.project='model-center' AND coalesce(e.run,'') LIKE 'model-test-%')",
            "events_without_run": "SELECT e.seq FROM events e LEFT JOIN runs r ON r.id=e.run WHERE e.run IS NOT NULL AND r.id IS NULL AND e.run NOT LIKE 'model-test-%'",
            "event_project_mismatch": "SELECT e.seq FROM events e JOIN runs r ON r.id=e.run WHERE e.project IS NOT NULL AND e.project IS NOT r.project",
            "candidates_without_project": "SELECT c.id FROM candidates c LEFT JOIN projects p ON p.id=c.project WHERE p.id IS NULL",
            "invalid_candidate_parent": "SELECT c.id FROM candidates c LEFT JOIN candidates p ON p.id=c.parent WHERE c.parent IS NOT NULL AND (p.id IS NULL OR p.project IS NOT c.project OR p.kind IS NOT c.kind)",
            "cases_without_project": "SELECT c.id FROM cases c LEFT JOIN projects p ON p.id=c.project WHERE p.id IS NULL",
            "skills_without_project": "SELECT s.id FROM skills s LEFT JOIN projects p ON p.id=s.project WHERE p.id IS NULL",
            "invalid_skill_task": "SELECT s.id FROM skills s LEFT JOIN tasks t ON t.id=s.task WHERE s.task IS NOT NULL AND (t.id IS NULL OR t.project IS NOT s.project)",
            "calls_without_run": "SELECT c.id FROM calls c LEFT JOIN runs r ON r.id=c.run WHERE c.run IS NOT NULL AND c.run NOT LIKE 'model-test-%' AND r.id IS NULL",
            "calls_without_project": "SELECT c.id FROM calls c LEFT JOIN projects p ON p.id=c.project WHERE p.id IS NULL AND NOT (coalesce(c.project,'')='model-center' AND coalesce(c.run,'') LIKE 'model-test-%')",
            "call_project_mismatch": "SELECT c.id FROM calls c JOIN runs r ON r.id=c.run WHERE c.project IS NOT r.project",
            "schedules_without_project": "SELECT s.project FROM schedules s LEFT JOIN projects p ON p.id=s.project WHERE p.id IS NULL",
            "decisions_without_task": "SELECT d.task FROM task_decisions d LEFT JOIN tasks t ON t.id=d.task WHERE t.id IS NULL",
            "revisions_without_task": "SELECT d.id FROM task_revisions d LEFT JOIN tasks t ON t.id=d.task WHERE t.id IS NULL",
            "snapshots_without_revision": "SELECT s.revision FROM task_revision_snapshots s LEFT JOIN task_revisions r ON r.id=s.revision WHERE r.id IS NULL",
            "invalid_dependencies": "SELECT d.task FROM task_dependencies d LEFT JOIN tasks t ON t.id=d.task LEFT JOIN tasks p ON p.id=d.dependency WHERE t.id IS NULL OR p.id IS NULL OR t.project IS NOT p.project OR t.id=p.id",
            "invalid_group_members": "SELECT m.group_task FROM task_group_members m LEFT JOIN tasks g ON g.id=m.group_task LEFT JOIN tasks t ON t.id=m.original_task WHERE g.id IS NULL OR t.id IS NULL OR g.project IS NOT t.project OR g.id=t.id",
            "invalid_submissions": "SELECT s.request FROM submissions s LEFT JOIN tasks t ON t.id=s.task WHERE t.id IS NULL OR s.project IS NOT t.project",
            "invalid_completions": "SELECT c.task FROM task_completions c LEFT JOIN tasks t ON t.id=c.task WHERE t.id IS NULL OR c.project IS NOT t.project",
            "phases_without_run": "SELECT p.id FROM phase_times p LEFT JOIN runs r ON r.id=p.run WHERE r.id IS NULL",
            "waits_without_run": "SELECT w.id FROM remote_waits w LEFT JOIN runs r ON r.id=w.run WHERE r.id IS NULL",
            "stages_without_run": "SELECT s.run FROM stages s LEFT JOIN runs r ON r.id=s.run WHERE r.id IS NULL",
        }
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Relationship audit limit must be 1..1000")
        result = {}
        truncated = {}
        for name, sql in checks.items():
            rows = self.query(sql + " LIMIT ?", (limit + 1,))
            result[name] = [row[next(iter(row))] for row in rows[:limit]]
            truncated[name] = len(rows) > limit
        result["_truncated"] = truncated
        result["_limit"] = limit
        return result

    def quarantine_orphans(self, limit=100):
        """Move provable orphan rows into an audit table without deleting evidence."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Relationship audit limit must be 1..1000")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT s.rowid AS original_rowid,s.* FROM stages s LEFT JOIN runs r ON r.id=s.run WHERE r.id IS NULL "
                              "ORDER BY s.run,s.stage,s.rowid LIMIT ?", (limit + 1,)).fetchall()
            for row in rows[:limit]:
                evidence = dict(row)
                original_rowid = evidence.pop("original_rowid")
                db.execute("INSERT INTO relationship_quarantine(table_name,row_id,data,created) VALUES ('stages',?,?,?)",
                           (str(original_rowid), encode(evidence), time.time()))
                db.execute("DELETE FROM stages WHERE rowid=?", (original_rowid,))
            return {"stages_without_run": min(len(rows), limit), "has_more": len(rows) > limit}

    def config_revision(self, project=None):
        row = self.one("SELECT value FROM settings WHERE key=?", ("config-revision:" + (project or "global"),))
        return int(row["value"]) if row and str(row["value"]).isdigit() else 0

    def bump_config_revision(self, project=None, expected=None):
        key = "config-revision:" + (project or "global")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            if expected is not None and int(expected) != current:
                raise ValueError("Configuration changed in another window; reload before saving")
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(current + 1)))
            return current + 1

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        try:
            return json.loads(row["value"]) if row else default
        except (ValueError, TypeError):
            # Configuration and credential registry corruption must reach their repair/rejection flows.
            if key in ('config', 'credential-cleanup-pending') or key.startswith('config:'):
                raise
            return default

    def put_namespaced_setting(self, namespace, key, value, version=1, ttl=None):
        """Explicit opt-in envelope; existing settings retain their wire format."""
        if not isinstance(namespace, str) or not namespace or ':' in namespace:
            raise ValueError("Invalid settings namespace")
        if not isinstance(key, str) or not key or type(version) is not int or version < 1:
            raise ValueError("Invalid settings key or version")
        if ttl is not None and (type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0):
            raise ValueError("Invalid settings TTL")
        if type(value) not in (dict, list, str, int, float, bool, type(None)):
            raise ValueError("Settings value must be a JSON type")
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("Settings value must be finite JSON") from exc
        self.put_setting('namespace:' + namespace + ':' + key,
                         {"version": version, "type": type(value).__name__, "value": value,
                          "expires": time.time() + ttl if ttl is not None else None})

    def namespaced_setting(self, namespace, key, expected_type, version=1, default=None, now=None):
        envelope = self.setting('namespace:' + namespace + ':' + key)
        if not isinstance(envelope, dict) or envelope.get('version') != version:
            return default
        value = envelope.get('value')
        if type(value) is not expected_type or envelope.get('type') != expected_type.__name__:
            return default
        expires = envelope.get('expires')
        if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or
                                    expires <= (time.time() if now is None else now)):
            return default
        return value

    def put_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    def reserve_case_exposure(self, project, case, experiment, limit):
        key = "case-exposure:" + experiment + ":" + case
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
                return key
            if db.execute("UPDATE cases SET uses=uses+1 WHERE id=? AND project=? AND uses<?",
                          (case, project, limit)).rowcount != 1:
                raise ValueError("Evaluation case exposure is unavailable or exhausted")
            db.execute("INSERT INTO settings VALUES (?,?)", (key, encode({"state": "possible_exposure",
                "case": case, "experiment": experiment, "created": time.time()})))
        return key

    def event(self, kind, message, project=None, run=None, data=None):
        self.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)",
                     (project, run, kind, message, encode(data or {}), time.time()))

    def add_task(self, project, source, key, title, body="", evidence=None, acceptance=None,
                 priority=50, overrides=None, request_id=None, request_signature=None):
        task = uid()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM settings WHERE key=?", ("project-archived:" + project,)).fetchone():
                raise ValueError("Project is archived; restore it before starting work")
            if request_id:
                prior = db.execute("SELECT * FROM submissions WHERE project=? AND request=?", (project, request_id)).fetchone()
                if prior:
                    if prior["signature"] != request_signature:
                        raise ValueError("Submission ID was already used with different content")
                    return prior["task"]
            db.execute("""INSERT OR IGNORE INTO tasks
                (id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,
                 overrides,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (task, project, source, key, title[:300], body[:30000], encode(evidence or []),
                        encode(acceptance or []), "queued", priority, encode(overrides or {}), now, now))
            row = db.execute("SELECT id FROM tasks WHERE project=? AND fingerprint=?", (project, key)).fetchone()
            if request_id:
                db.execute("INSERT INTO submissions VALUES (?,?,?,?)", (project, request_id, request_signature, row["id"]))
        return row["id"]

    def defer(self, task, decision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO task_decisions VALUES (?,?,?)", (task, encode(decision), time.time()))
            db.execute("UPDATE tasks SET state='deferred',updated=? WHERE id=?", (time.time(), task))

    def decide(self, task, state, decision):
        """批量初筛决定落库：决定与理由写入 task_decisions，状态仅从排队转变。

        同指纹的既有任务（如已完成、过时）不被覆盖；此时也不写决定，
        与升级前「守卫失败即无可见变化」的行为一致。
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE tasks SET state=?,updated=? WHERE id=? AND state='queued'",
                                 (state, time.time(), task)).rowcount
            if changed:
                db.execute("INSERT OR REPLACE INTO task_decisions VALUES (?,?,?)",
                           (task, encode(decision), time.time()))
        return bool(changed)

    def phase(self, run, name):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE id=? AND ended IS NULL", (run,)).fetchone():
                return
            active = db.execute("SELECT phase FROM phase_times WHERE run=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (run,)).fetchone()
            if not active or active["phase"] != name:
                self._end_spans(db, "phase_times", run, now)
                cursor = db.execute("INSERT INTO phase_times(run,phase,started) VALUES (?,?,?)", (run, name, now))
                self._clock_samples[("phase_times", cursor.lastrowid)] = time.monotonic()

    def _end_spans(self, db, table, run, now):
        if table not in ("phase_times", "remote_waits"):
            raise ValueError("Invalid span table")
        for row in db.execute("SELECT id,started FROM " + table + " WHERE run=? AND ended IS NULL", (run,)).fetchall():
            began = self._clock_samples.pop((table, row["id"]), None)
            elapsed = max(0, time.monotonic() - began if began is not None else now - row["started"])
            db.execute("UPDATE " + table + " SET ended=?,duration=? WHERE id=?", (now, elapsed, row["id"]))

    def remote_wait(self, run, reason, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT reason FROM remote_waits WHERE run=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (run,)).fetchone()
            if active and active["reason"] == reason:
                return
            self._end_spans(db, "remote_waits", run, now)
            if reason:
                cursor = db.execute("INSERT INTO remote_waits(run,reason,started) VALUES (?,?,?)", (run, reason, now))
                self._clock_samples[("remote_waits", cursor.lastrowid)] = time.monotonic()

    @staticmethod
    def queue_order(display=False):
        rank = "(priority + min(20, (? - created)/86400.0))"
        if display:
            return ("CASE WHEN " + Store.ready_clause() + " THEN 0 WHEN state='queued' THEN 1 ELSE 2 END,"
                    "CASE WHEN state='queued' THEN urgency ELSE 0 END DESC,"
                    "CASE WHEN state='queued' THEN " + rank + " ELSE NULL END DESC,"
                    "CASE WHEN state='queued' THEN created ELSE -updated END,id")
        return "urgency DESC," + rank + " DESC,created,id"

    @staticmethod
    def ready_clause():
        return ("state='queued' AND available<=? AND NOT EXISTS "
                "(SELECT 1 FROM task_dependencies d LEFT JOIN tasks p ON p.id=d.dependency "
                "WHERE d.task=tasks.id AND (p.id IS NULL OR p.state<>'merged'))")

    def claim(self, project, config):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            p = db.execute("SELECT paused FROM projects WHERE id=?", (project,)).fetchone()
            busy = db.execute("SELECT 1 FROM runs WHERE project=? AND ended IS NULL", (project,)).fetchone()
            archived = db.execute("SELECT 1 FROM settings WHERE key=?", ("project-archived:" + project,)).fetchone()
            if not p or p["paused"] or busy or archived:
                return None
            from .config import merge, validate
            task = None
            effective = None
            recent = db.execute("SELECT t.source FROM runs r JOIN tasks t ON t.id=r.task "
                                "WHERE r.project=? ORDER BY r.started DESC,r.id DESC LIMIT 3", (project,)).fetchall()
            fair_order = ("CASE WHEN source='system_delivery' THEN 1 ELSE 0 END," if
                          len(recent) == 3 and all(row["source"] == "system_delivery" for row in recent) else "")
            while True:
                candidate = db.execute("SELECT * FROM tasks WHERE project=? AND " + self.ready_clause() + " "
                                       "ORDER BY " + fair_order + self.queue_order() + " LIMIT 1",
                                       (project, now, now)).fetchone()
                if not candidate:
                    return None
                source_key = {"system_light": "inspection", "system_issues": "issues"}.get(candidate["source"])
                if (source_key and not config["sources"][source_key] and
                        any(isinstance(item, dict) and item.get("scan_trigger") == "automatic"
                            for item in json.loads(candidate["evidence"] or "[]"))):
                    db.execute("UPDATE tasks SET state='obsolete',updated=? WHERE id=?", (now, candidate["id"]))
                    continue
                try:
                    overrides = json.loads(candidate["overrides"])
                    if not isinstance(overrides, dict):
                        raise ValueError("Task configuration overrides must be an object")
                    effective = validate(merge(config, overrides))
                except ZoneInfoNotFoundError:
                    reason = "Unknown schedule timezone"
                except (ValueError, TypeError, AttributeError) as exc:
                    reason = str(exc)[:300] or "Invalid task configuration overrides"
                else:
                    task = candidate
                    break
                db.execute("UPDATE tasks SET state='blocked',updated=? WHERE id=? AND state='queued'",
                           (now, candidate["id"]))
                db.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)",
                           (project, None, "task_configuration_error",
                            "Task was blocked because its configuration overrides are invalid: " + reason,
                            encode({"task": candidate["id"], "reason": reason}), now))
                continue
            run = uid()
            db.execute("UPDATE tasks SET state='planning',attempts=attempts+1,updated=? WHERE id=?", (now, task["id"]))
            db.execute("INSERT INTO runs(id,task,project,state,config,started) VALUES (?,?,?,?,?,?)",
                       (run, task["id"], project, "planning", encode(effective), now))
            cursor = db.execute("INSERT INTO phase_times(run,phase,started) VALUES (?,?,?)", (run, "planning", now))
            self._clock_samples[("phase_times", cursor.lastrowid)] = time.monotonic()
        return self.one("SELECT * FROM runs WHERE id=?", (run,))

    def transition(self, run, state, result=None, terminal=False, task_fields=None):
        """Persist a transition, resolving stop requests atomically; return whether a write occurred."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
            if not row or row["ended"] is not None:
                return False
            task = db.execute("SELECT state FROM tasks WHERE id=?", (row["task"],)).fetchone()
            # 已发出的远端操作保留事实；其余状态和自动重试必须服从同事务读取的停止请求。
            if state not in ["merged", "pr_open"]:
                if task and task["state"] == "cancelled":
                    state, terminal, task_fields = "cancelled", True, None
                    result = {**(result or {}), "reason": "Task cancellation requested"}
                elif db.execute("SELECT 1 FROM settings WHERE key=?", ("run-stop:" + run,)).fetchone():
                    state, terminal, task_fields = "interrupted", True, None
                    result = {**(result or {}), "reason": "Project pause requested; retry creates a fresh run"}
            previous = json.loads(row["result"])
            phase = (result or {}).get("stage", state)
            active_phase = db.execute("SELECT phase FROM phase_times WHERE run=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (run,)).fetchone()
            if not active_phase or active_phase["phase"] != phase or terminal:
                self._end_spans(db, "phase_times", run, now)
                if terminal:
                    self._end_spans(db, "remote_waits", run, now)
                if not terminal:
                    cursor = db.execute("INSERT INTO phase_times(run,phase,started) VALUES (?,?,?)", (run, phase, now))
                    self._clock_samples[("phase_times", cursor.lastrowid)] = time.monotonic()
            result = {**{key: previous[key] for key in ["value", "scope", "plan_reason", "plan_decision", "failure", "stage"] if key in previous}, **(result or {})}
            db.execute("UPDATE runs SET state=?,result=?,ended=? WHERE id=?",
                       (state, encode(result or {}), now if terminal else None, run))
            # task_fields 必须与状态变更同事务写入（如 waiting_budget 的退避时间），
            # 否则调度器可能在两条语句之间按旧值把任务立即重排。
            extra = ""
            values = []
            if task_fields and task and task["state"] != "cancelled":
                allowed_fields = {"state", "available", "infra_retries", "attempts", "budget_waits"}
                safe_fields = {k: v for k, v in task_fields.items() if k in allowed_fields}
                if safe_fields:
                    extra = "," + ",".join(field + "=?" for field in safe_fields)
                    values = [safe_fields[field] for field in safe_fields]
            db.execute("UPDATE tasks SET state=?,updated=?" + extra + " WHERE id=?",
                       (state, now, *values, row["task"]))
            if state == "deferred":
                db.execute("INSERT OR REPLACE INTO task_decisions VALUES (?,?,?)", (row["task"], encode(result), now))
            if terminal and state in ("awaiting_approval", "pr_open", "merged", "completed"):
                db.execute("INSERT OR IGNORE INTO task_completions SELECT id,project,? FROM tasks "
                           "WHERE id=? AND source NOT LIKE 'system_%'", (now, row["task"]))
            db.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)",
                       (row["project"], run, state, (result or {}).get("reason", state), encode(result or {}), now))
        return True

    def recover(self):
        rows = self.query("SELECT id FROM runs WHERE ended IS NULL")
        for row in rows:
            self.transition(row["id"], "interrupted", {"reason": "Runtime restarted; artifacts retained. Retry creates a fresh isolated run."}, True)
        self.execute("UPDATE calls SET state='interrupted' WHERE state='reserved'")
        self.execute("UPDATE candidates SET status='interrupted' WHERE status IN ('proposing','evaluating')")
        return len(rows)

    def catalog_entry(self, project, provider_id, signature, legacy_signature):
        """First persisted catalog entry for an identity; returns (value, from_legacy_key)."""
        for key, legacy in catalog_keys(project, provider_id, signature, legacy_signature):
            value = self.setting(key)
            if isinstance(value, dict):
                return value, legacy
        return None, False

    def reserve_call(self, project, run, role, route, config):
        now = time.time()
        from .config import BUILTIN_PROVIDERS
        provider_id = route.get("provider", "openai")
        provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
        provider = provider or next((p for p in BUILTIN_PROVIDERS if p["id"] == provider_id), {"id": provider_id})
        # 与模型中心目录使用同一身份（含该供应商自己的凭据代次）；冻结运行仍按
        # 其封存配置里的供应商解析，不套用当前配置的代次。
        signature = provider_identity(provider, credential_generation(self.root, provider))
        directory, _ = self.catalog_entry(project, provider_id, signature, provider_identity(provider))
        models = directory.get("models") if isinstance(directory, dict) else None
        entry = next((item for item in (models if isinstance(models, list) else [])
                      if isinstance(item, dict) and item.get("id") == route["model"]), {})
        price = entry.get("unit_price")
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            price = float(price)
            if not math.isfinite(price) or not 0 <= price <= 1000000:
                price = None
        else:
            price = None
        from .usage import unit_prices
        prices = unit_prices(entry.get("prices"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = uid()
            db.execute("INSERT INTO calls(id,project,run,role,model,effort,state,created,provider,unit_price,unit,"
                       "input_price,output_price,cache_read_price,cache_write_price,price_basis) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (call, project, run, role, route["model"], route["effort"], "reserved", now, provider_id, price,
                        "opencode_session" if provider.get("protocol") == "opencode" else
                        "codex_session" if provider.get("protocol", "codex") in ["codex", "codex_login"] else "http_request",
                        prices.get("input"), prices.get("output"), prices.get("cache_read"),
                        prices.get("cache_write"), None))
            db.execute("UPDATE runs SET calls=calls+1 WHERE id=?", (run,))
        return call

    def record_usage(self, call, run, details, seconds):
        values = details or {"tokens": 0, "input_tokens": 0, "output_tokens": 0,
                             "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_known": False}
        fields = ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "usage_known"]
        from .usage import PRICE_FIELDS, estimate, unit_prices
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT tokens,run,unit_price,input_price,output_price,"
                                  "cache_read_price,cache_write_price FROM calls WHERE id=?", (call,)).fetchone()
            if previous is None or previous["run"] != run:
                raise ValueError("Usage call does not belong to run")
            prices = unit_prices({field: previous[field + "_price"] for field in PRICE_FIELDS})
            _, basis = estimate(values, prices, previous["unit_price"])
            db.execute("UPDATE calls SET " + ",".join(field + "=?" for field in fields) +
                       ",seconds=?,price_basis=? WHERE id=?",
                       (*[values[field] for field in fields], seconds, basis, call))
            db.execute("UPDATE runs SET tokens=max(0,tokens+?) WHERE id=?",
                       (values["tokens"] - previous["tokens"], run))
