import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError


def uid():
    return uuid.uuid4().hex[:16]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "state.sqlite3"
        with self.connect() as db:
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
            """)
            db.execute("BEGIN IMMEDIATE")
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

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
                 priority=50, overrides=None):
        task = uid()
        now = time.time()
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO tasks
                (id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,
                 overrides,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (task, project, source, key, title[:300], body[:30000], encode(evidence or []),
                        encode(acceptance or []), "queued", priority, encode(overrides or {}), now, now))
            row = db.execute("SELECT id FROM tasks WHERE project=? AND fingerprint=?", (project, key)).fetchone()
        return row["id"]

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
            while True:
                candidate = db.execute("SELECT * FROM tasks WHERE project=? AND state='queued' AND available<=? "
                                       "ORDER BY (priority + min(20, (? - created)/86400.0)) DESC,created,id LIMIT 1",
                                       (project, now, now)).fetchone()
                if not candidate:
                    return None
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
        return self.one("SELECT * FROM runs WHERE id=?", (run,))

    def transition(self, run, state, result=None, terminal=False):
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
            if not row or row["ended"] is not None:
                return False
            previous = json.loads(row["result"])
            result = {**{key: previous[key] for key in ["value", "scope", "plan_reason", "plan_decision", "failure", "stage"] if key in previous}, **(result or {})}
            db.execute("UPDATE runs SET state=?,result=?,ended=? WHERE id=?",
                       (state, encode(result or {}), now if terminal else None, run))
            db.execute("UPDATE tasks SET state=?,updated=? WHERE id=?", (state, now, row["task"]))
        self.event(state, (result or {}).get("reason", state), row["project"], run, result)
        return True

    def recover(self):
        rows = self.query("SELECT id FROM runs WHERE ended IS NULL")
        for row in rows:
            self.transition(row["id"], "interrupted", {"reason": "Runtime restarted; artifacts retained. Retry creates a fresh isolated run."}, True)
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
            previous = db.execute("SELECT tokens FROM calls WHERE id=?", (call,)).fetchone()
            db.execute("UPDATE calls SET " + ",".join(field + "=?" for field in fields) + ",seconds=? WHERE id=?",
                       (*[values[field] for field in fields], seconds, call))
            db.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (values["tokens"] - previous["tokens"], run))
