"""Ordered additive migrations with explicitly identified legacy checksums."""
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time


LOGGER = logging.getLogger(__name__)

SCRIPTS = ((3, "003_seal_bootstrap.sql"), (4, "004_revision_snapshots.sql"),
           (5, "005_task_group_lineage.sql"), (6, "006_dependency_dag.sql"),
           (7, "007_call_prices.sql"), (8, "008_intervention_history_index.sql"),
           (9, "009_open_runs_index.sql"), (10, "010_failure_monitor_outbox.sql"))

# _bootstrap() 的建表 DDL：executescript 会隐式提交，与后续补列/数据迁移事务分离，
# 进程可能在两者之间中断。提取为常量供 Store 复用，也让回归测试能直接构造
# 「只完成建表、未补列、账本为空」的中断状态。
BOOTSTRAP_SCHEMA = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied REAL NOT NULL);
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
    -- 调度循环每 tick 用 `runs WHERE ended IS NULL` 查活跃 run；部分索引只覆盖
    -- 未结束行，run 数增长时不再全表扫描。
    CREATE INDEX IF NOT EXISTS open_runs ON runs(project) WHERE ended IS NULL;
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
    CREATE TABLE IF NOT EXISTS request_log (
        id TEXT PRIMARY KEY, method TEXT, path TEXT, status INTEGER, started REAL,
        ended REAL, error_code TEXT);
    CREATE TABLE IF NOT EXISTS relationship_quarantine (
        id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT, row_id TEXT, data TEXT, created REAL);
    CREATE INDEX IF NOT EXISTS request_log_started ON request_log(started DESC);
    CREATE INDEX IF NOT EXISTS project_completions ON task_completions(project);
    CREATE INDEX IF NOT EXISTS event_kind_created ON events(kind, created);
    CREATE INDEX IF NOT EXISTS phase_times_run ON phase_times(run);
    CREATE INDEX IF NOT EXISTS remote_waits_run ON remote_waits(run);
    CREATE INDEX IF NOT EXISTS task_revisions_task ON task_revisions(task);
    CREATE INDEX IF NOT EXISTS task_dependencies_dependency ON task_dependencies(dependency);
    CREATE INDEX IF NOT EXISTS runs_task_state ON runs(task, state);
"""

# _bootstrap() 建表之后、写账本之前在显式事务里补齐的必需列全集：
# 只有它们全部就位才算 bootstrap 完成（版本 1 的证据）。
BOOTSTRAP_EVIDENCE_COLUMNS = {
    "tasks": ("urgency", "infra_retries", "budget_waits"),
    "calls": ("provider", "unit_price", "unit", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "usage_known"),
    "phase_times": ("duration",),
    "remote_waits": ("duration",),
}

# 账本缺行时用于判断迁移是否真的执行过的可见痕迹：说明 + 只读检查。
# 缺失的版本只能补账本、不能重放迁移，因此每一项都必须能由 schema 自身核对。
EVIDENCE = {
    1: ("the complete bootstrap effect: tasks urgency/infra_retries/budget_waits, calls "
        "provider/unit_price/unit/usage columns, phase_times/remote_waits duration",
        lambda db, tables, indexes: all(set(columns) <= set(_columns(db, table))
                                        for table, columns in BOOTSTRAP_EVIDENCE_COLUMNS.items())),
    2: ("observability tables request_log, phase_times, remote_waits, task_completions",
        lambda db, tables, indexes: {"request_log", "phase_times", "remote_waits", "task_completions"} <= tables),
    3: ("the removed request_log_created index",
        lambda db, tables, indexes: "request_log" in tables and "request_log_created" not in indexes),
    4: ("table task_revision_snapshots",
        lambda db, tables, indexes: "task_revision_snapshots" in tables),
    5: ("table task_group_members with index task_group_original",
        lambda db, tables, indexes: "task_group_members" in tables and "task_group_original" in indexes),
    6: ("table task_dependencies(task,dependency) keyed on both columns",
        lambda db, tables, indexes: (_columns(db, "task_dependencies").get("task"),
                                     _columns(db, "task_dependencies").get("dependency")) == ((1, 1), (1, 2))),
    7: ("calls price columns input_price, output_price, cache_read_price, cache_write_price, price_basis",
        lambda db, tables, indexes: {"input_price", "output_price", "cache_read_price", "cache_write_price",
                                     "price_basis"} <= set(_columns(db, "calls"))),
    8: ("index events_intervention_task on events(project, task, seq)",
        lambda db, tables, indexes: "events_intervention_task" in indexes),
    9: ("partial index open_runs on runs(project) where ended is null",
        lambda db, tables, indexes: "open_runs" in indexes),
    10: ("table failure_monitor_outbox and its ordering index",
         lambda db, tables, indexes: "failure_monitor_outbox" in tables and "failure_monitor_outbox_order" in indexes),
}


def scripts():
    folder = Path(__file__).with_name("migrations")
    return [(version, (folder / filename).read_bytes()) for version, filename in SCRIPTS]


def _expected():
    expected = {version: hashlib.sha256(("adaptive-rsi-schema-v" + str(version)).encode()).hexdigest()
                for version in (1, 2)}
    expected.update({version: hashlib.sha256(content).hexdigest() for version, content in scripts()})
    return expected


def has_ledger(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone() is not None


def _history(db):
    """Return the expected checksums and applied versions, rejecting foreign or rewritten history."""
    expected = _expected()
    applied = set()
    for row in db.execute("SELECT version,checksum FROM schema_migrations"):
        version = row["version"]
        if version not in expected:
            raise RuntimeError("Database migration history is newer than this runtime")
        if row["checksum"] != expected[version]:
            raise RuntimeError("Database migration checksum mismatch")
        applied.add(version)
    return expected, applied


def _tables(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _columns(db, table):
    # (notnull, pk) 足以区分 006 前后的单列主键与复合主键，无需读取数据。
    return {row["name"]: (row["notnull"], row["pk"]) for row in db.execute("PRAGMA table_info(" + table + ")")}


def _effect_visible(db, version, tables, indexes):
    # 未登记判据的版本（新增迁移还没补充痕迹检查）一律不可确认，交给 apply() 正常执行。
    evidence = EVIDENCE.get(version)
    return bool(evidence and evidence[1](db, tables, indexes))


def heal(db):
    """Backfill ledger rows for migrations whose effects are already visible.

    只写账本行，不重放迁移、不改动业务数据。缺口处看不到对应痕迹、却已经记录了
    更高版本时抛错：这种状态无法确认是中断还是人工删行，继续启动会掩盖问题，
    应当由维护者核对或恢复备份。

    Returns the backfilled versions.
    """
    if not has_ledger(db):
        return ()
    expected, applied = _history(db)
    tables, indexes = _tables(db), _indexes(db)
    highest = max(applied, default=0)
    healed = []
    for version in sorted(expected):
        if version in applied:
            continue
        if not _effect_visible(db, version, tables, indexes):
            # 从这个版本起没有任何生效痕迹：交给 apply() 按顺序正常执行。
            # 但账本已经记录了更高版本时状态无法确认，明确报错而不是静默跳过。
            if highest > version:
                raise RuntimeError(
                    "Database migration history has a gap at version %d: %s is missing, but version %d is "
                    "recorded; restore a backup or repair the ledger manually"
                    % (version, EVIDENCE.get(version, ("the migration schema effect",))[0], highest))
            break
        # 并发 opener 可能刚补过同一行：忽略主键冲突，且只把自己写入的版本计入返回值，
        # 避免日志声称回填了别人已经补好的账。
        cursor = db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)",
                            (version, expected[version], time.time()))
        if cursor.rowcount:
            healed.append(version)
    if healed:
        LOGGER.warning("Backfilled migration ledger rows for version(s) %s without replaying migrations",
                       ", ".join(str(version) for version in healed))
    return tuple(healed)


def validate_history(db):
    if not has_ledger(db):
        return False
    _, applied = _history(db)
    if applied and set(range(1, max(applied) + 1)) != applied:
        raise RuntimeError("Database migration history has a gap")
    return 3 in applied


def _quarantine_unsafe_dependencies(db):
    """Archive legacy dependency rows that migration 006 cannot copy.

    006 重建 task_dependencies（NOT NULL、复合主键、CHECK(task<>dependency)），随后裸
    INSERT ... SELECT 复制旧表。历史库里只要有一行自环或指向已删任务，迁移就抛原始
    SQLite 错误且每次启动重复失败；清理这些行的 quarantine_orphans 只在启动后的
    maintenance 里跑，永远到不了。这里在 006 执行前把恰好无法满足新约束的行搬进
    relationship_quarantine 并各记一条事件；006 文件本身不动，正常库逐行原样复制。

    行存在但无法安全归档时抛带版本号与修复指引的 RuntimeError；不静默丢弃证据。
    """
    tables = _tables(db)
    if "task_dependencies" not in tables or not {"task", "dependency"} <= set(
            _columns(db, "task_dependencies")):
        raise RuntimeError(
            "Database migration 6 cannot inspect task_dependencies: the legacy table or its "
            "task/dependency columns are missing; restore a backup or repair the table, then reopen")
    rows = db.execute(
        "SELECT d.rowid AS original_rowid,d.task,d.dependency,t.project AS task_project "
        "FROM task_dependencies d LEFT JOIN tasks t ON t.id=d.task "
        "LEFT JOIN tasks p ON p.id=d.dependency "
        "WHERE d.task IS NULL OR d.dependency IS NULL OR d.task=d.dependency "
        "OR t.id IS NULL OR p.id IS NULL ORDER BY d.rowid").fetchall()
    if not rows:
        return 0
    missing = [name for name in ("relationship_quarantine", "events") if name not in tables]
    if missing:
        raise RuntimeError(
            "Database migration 6 found %d task_dependencies row(s) that violate the new "
            "dependency constraints but cannot quarantine them: table(s) %s are missing; "
            "restore a backup or create the missing table(s), then reopen"
            % (len(rows), ", ".join(missing)))
    now = time.time()
    try:
        for row in rows:
            data = json.dumps({"task": row["task"], "dependency": row["dependency"]},
                              ensure_ascii=False, sort_keys=True)
            db.execute("INSERT INTO relationship_quarantine(table_name,row_id,data,created) "
                       "VALUES ('task_dependencies',?,?,?)",
                       (str(row["original_rowid"]), data, now))
            db.execute("DELETE FROM task_dependencies WHERE rowid=?", (row["original_rowid"],))
            db.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,NULL,?,?,?,?)",
                       (row["task_project"], "dependency_orphan_quarantined",
                        "Orphaned dependency reference quarantined; task readiness re-evaluated",
                        data, now))
    except sqlite3.Error as exc:
        raise RuntimeError(
            "Database migration 6 found %d task_dependencies row(s) that violate the new "
            "dependency constraints but could not be quarantined (%s); restore a backup or "
            "repair the quarantine tables, then reopen" % (len(rows), exc)) from exc
    LOGGER.warning("Quarantined %d task_dependencies row(s) before migration 6", len(rows))
    return len(rows)


def _ledger_has(db, version):
    return db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone() is not None


def apply(db):
    for version, content in scripts():
        # 锁外的预检只用来跳过已应用版本；真正判定必须在写锁内复查：另一个进程可能在
        # 预检与 BEGIN IMMEDIATE 之间提交同一迁移。缺少这次复查时会重放迁移体，非幂等
        # DDL 直接报 table already exists，随后账本写入才报 UNIQUE，把并发伪装成坏库。
        if _ledger_has(db, version):
            continue
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        if _ledger_has(db, version):
            LOGGER.info("Migration %d was applied by another opener while waiting for the write lock; "
                        "skipping its body", version)
            continue
        if version == 6:
            # 006 以文件 sha256 记账，不能在 SQL 里加过滤；预清理与迁移共用同一事务，
            # 006 失败时隔离记录一并回滚，坏库不会留下半个状态。
            _quarantine_unsafe_dependencies(db)
        # executescript commits implicitly; execute complete statements within the
        # same transaction as the ledger row so interrupted migrations roll back.
        statement = ""
        for line in content.decode("utf-8").splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("Incomplete database migration statement")
        try:
            recorded = db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)",
                                  (version, hashlib.sha256(content).hexdigest(), time.time())).rowcount
        except sqlite3.IntegrityError:
            # OR IGNORE 之外的约束错误：仅当该版本已由并发路径记账时才视为成功。
            if not _ledger_has(db, version):
                raise
            recorded = 0
        if not recorded:
            # 写锁内复查已排除普通并发；走到这里说明账本行由本事务之外的路径写入
            # （例如另一进程的 heal 补账）。迁移体已执行，不回滚、不重放。
            LOGGER.warning("Migration %d ledger row was written by another opener; keeping its checksum",
                           version)
