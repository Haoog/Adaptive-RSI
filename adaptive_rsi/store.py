import contextlib
import json
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError


# /api/state 单次请求会执行十几条查询；8 个复用连接足以覆盖线程池的并发峰值，
# 池满时新借出的连接用完直接关闭，不阻塞调用方。
POOL_SIZE = 8


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


def decode_evidence(value):
    """Validate containers consumed by historical dashboards, preserving raw database evidence."""
    result = decode_object(value)
    for field in ("review", "evaluation", "feedback", "failure", "automation", "value", "monitor"):
        if field in result and not isinstance(result[field], dict):
            return {"evidence_error": "invalid_evidence_shape:" + field}
    review, evaluation = result.get("review", {}), result.get("evaluation", {})
    findings, trials, checks = review.get("findings", []), result.get("trials", []), evaluation.get("checks", {})
    for name, rows in (("findings", findings), ("trials", trials)):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return {"evidence_error": "invalid_evidence_shape:" + name}
    if any("case" in row and not isinstance(row["case"], (str, type(None))) for row in trials):
        return {"evidence_error": "invalid_evidence_shape:case"}
    if not isinstance(checks, dict) or any(not isinstance(rows, list) or
            any(not isinstance(row, dict) for row in rows) for rows in checks.values()):
        return {"evidence_error": "invalid_evidence_shape:checks"}
    if "merged_at" in result and not isinstance(result["merged_at"], (int, float)):
        return {"evidence_error": "invalid_evidence_shape:merged_at"}
    return result


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
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise RuntimeError("Database schema is newer than this runtime")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT, path TEXT UNIQUE, config TEXT,
                    paused INTEGER DEFAULT 1, created REAL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), source TEXT,
                    fingerprint TEXT, title TEXT, body TEXT, evidence TEXT, acceptance TEXT,
                    state TEXT, priority INTEGER DEFAULT 50, attempts INTEGER DEFAULT 0,
                    overrides TEXT DEFAULT '{}', available REAL DEFAULT 0, created REAL, updated REAL,
                    UNIQUE(project, fingerprint));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task TEXT REFERENCES tasks(id), project TEXT,
                    state TEXT, config TEXT, strategy TEXT, base TEXT, path TEXT,
                    calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, started REAL, ended REAL,
                    result TEXT DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, run TEXT,
                    kind TEXT, message TEXT, data TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS schedules (
                    project TEXT, kind TEXT, due REAL, fingerprint TEXT DEFAULT '',
                    PRIMARY KEY(project,kind));
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, project TEXT, kind TEXT, parent TEXT, status TEXT,
                    content TEXT, evidence TEXT DEFAULT '{}', created REAL, promoted REAL);
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY, project TEXT, task TEXT, status TEXT, triggers TEXT,
                    content TEXT, evidence TEXT, expires REAL, created REAL);
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, project TEXT, family TEXT, split TEXT,
                    spec TEXT, uses INTEGER DEFAULT 0, created REAL,
                    UNIQUE(project,family));
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, project TEXT, run TEXT, role TEXT, model TEXT, effort TEXT,
                    state TEXT, tokens INTEGER DEFAULT 0, seconds REAL DEFAULT 0, created REAL,
                    provider TEXT DEFAULT 'openai');
                CREATE TABLE IF NOT EXISTS stages (
                    run TEXT REFERENCES runs(id), stage TEXT, signature TEXT, data TEXT, created REAL,
                    PRIMARY KEY(run,stage));
                CREATE INDEX IF NOT EXISTS project_tasks ON tasks(project,updated DESC);
                CREATE INDEX IF NOT EXISTS project_runs ON runs(project,started DESC);
                CREATE INDEX IF NOT EXISTS task_runs ON runs(task,started DESC,id DESC);
                CREATE INDEX IF NOT EXISTS delivery_runs ON runs(state,project);
                CREATE INDEX IF NOT EXISTS project_calls ON calls(project,created DESC);
                CREATE INDEX IF NOT EXISTS project_events ON events(project,seq DESC);
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(state,priority,available);
                CREATE TABLE IF NOT EXISTS task_decisions (
                    task TEXT PRIMARY KEY REFERENCES tasks(id), data TEXT NOT NULL, created REAL);
                CREATE TABLE IF NOT EXISTS task_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT REFERENCES tasks(id),
                    previous_body TEXT, supplement TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task TEXT PRIMARY KEY REFERENCES tasks(id), dependency TEXT REFERENCES tasks(id));
                CREATE TABLE IF NOT EXISTS submissions (
                    project TEXT, request TEXT, signature TEXT, task TEXT REFERENCES tasks(id),
                    PRIMARY KEY(project,request));
                CREATE TABLE IF NOT EXISTS phase_times (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT REFERENCES runs(id), phase TEXT,
                    started REAL, ended REAL);
                CREATE TABLE IF NOT EXISTS remote_waits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT REFERENCES runs(id), reason TEXT,
                    started REAL, ended REAL);
                CREATE TABLE IF NOT EXISTS task_completions (
                    task TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, project TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS project_completions ON task_completions(project);
                CREATE INDEX IF NOT EXISTS event_kind_created ON events(kind, created);
                CREATE INDEX IF NOT EXISTS phase_times_run ON phase_times(run);
                CREATE INDEX IF NOT EXISTS remote_waits_run ON remote_waits(run);
                CREATE INDEX IF NOT EXISTS task_revisions_task ON task_revisions(task);
            """)
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
            db.execute("PRAGMA user_version=1")

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
        db.identity = self._file_identity()
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _prepare(db):
        # 调用方可能在持有期间改过连接级设置，每次借出重新确保生效（PRAGMA 幂等）。
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextlib.contextmanager
    def connect(self):
        with self._pool_lock:
            generation = self._pool_generation
            try:
                db = self._pool.get_nowait()
            except queue.Empty:
                db = None
        if db is not None and db.identity != self._file_identity():
            # 升级回滚会整体替换 state.sqlite3（连同 -wal/-shm）；指向旧文件的
            # 池连接必须弃用，否则轻则 I/O 错误，重则读到替换前的数据。
            db.close()
            db = None
        if db is None:
            db = self._new_connection()
        try:
            self._prepare(db)
        except sqlite3.Error:
            # 借出瞬间文件仍可能被替换；首条语句失败的连接直接重建一次。
            db.close()
            db = self._new_connection()
            self._prepare(db)
        try:
            yield db
            db.commit()
        except BaseException:
            # 异常退出（包括 stream 被中途放弃）的连接可能持有坏状态：
            # 回滚后直接关闭，绝不归还回池。
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            db.close()
            raise
        else:
            with self._pool_lock:
                if generation != self._pool_generation:
                    db.close()
                else:
                    try:
                        self._pool.put_nowait(db)
                    except queue.Full:
                        db.close()

    def close(self):
        """关闭池中空闲连接；已借出的连接由各自的 with 块负责关闭。"""
        with self._pool_lock:
            self._pool_generation += 1
            while True:
                try:
                    self._pool.get_nowait().close()
                except queue.Empty:
                    return

    def query(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def stream(self, sql, args=()):
        """Yield rows without materializing an unbounded result set."""
        with self.connect() as db:
            for row in db.execute(sql, args):
                yield dict(row)

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def put_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    def event(self, kind, message, project=None, run=None, data=None):
        self.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)",
                     (project, run, kind, message, encode(data or {}), time.time()))

    def add_task(self, project, source, key, title, body="", evidence=None, acceptance=None,
                 priority=50, overrides=None, request_id=None, request_signature=None):
        task = uid()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
                "(SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.dependency "
                "WHERE d.task=tasks.id AND p.state<>'merged')")

    def claim(self, project, config):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            p = db.execute("SELECT paused FROM projects WHERE id=?", (project,)).fetchone()
            busy = db.execute("SELECT 1 FROM runs WHERE project=? AND ended IS NULL", (project,)).fetchone()
            if not p or p["paused"] or busy:
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
                extra = "," + ",".join(field + "=?" for field in task_fields)
                values = [task_fields[field] for field in task_fields]
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

    def reserve_call(self, project, run, role, route, config):
        now = time.time()
        from .config import BUILTIN_PROVIDERS, fingerprint
        provider_id = route.get("provider", "openai")
        provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
        provider = provider or next((p for p in BUILTIN_PROVIDERS if p["id"] == provider_id), {"id": provider_id})
        signature = fingerprint({k: v for k, v in provider.items() if k != "name"})
        prefix = "model-center:"
        directory = self.setting(prefix + str(project) + ":" + provider_id + ":" + signature)
        if directory is None:
            directory = self.setting(prefix + "global:" + provider_id + ":" + signature, {})
        price = next((m.get("unit_price") for m in directory.get("models", []) if m["id"] == route["model"]), None)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            call = uid()
            db.execute("INSERT INTO calls(id,project,run,role,model,effort,state,created,provider,unit_price,unit) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (call, project, run, role, route["model"], route["effort"], "reserved", now, provider_id, price,
                        "opencode_session" if provider.get("protocol") == "opencode" else
                        "codex_session" if provider.get("protocol", "codex") in ["codex", "codex_login"] else "http_request"))
            db.execute("UPDATE runs SET calls=calls+1 WHERE id=?", (run,))
        return call

    def record_usage(self, call, run, details, seconds):
        values = details or {"tokens": 0, "input_tokens": 0, "output_tokens": 0,
                             "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_known": False}
        fields = ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "usage_known"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT tokens,run FROM calls WHERE id=?", (call,)).fetchone()
            if previous is None or previous["run"] != run:
                raise ValueError("Usage call does not belong to run")
            db.execute("UPDATE calls SET " + ",".join(field + "=?" for field in fields) + ",seconds=? WHERE id=?",
                       (*[values[field] for field in fields], seconds, call))
            db.execute("UPDATE runs SET tokens=max(0,tokens+?) WHERE id=?",
                       (values["tokens"] - previous["tokens"], run))
