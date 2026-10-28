"""Background artifact retention and bounded state maintenance."""
import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path
import os
import stat
import uuid


CLEANUP_NAMES = ("workspace", "candidate", "baseline", "evaluation", "experiments",
                 "dependencies", "acceptance-design", "feedback")


def preview_cleanup(store, run_id):
    """Preview only finished, unheld direct run artifacts; never inspect source content."""
    run = store.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if (not run or run['ended'] is None or run['state'] not in
            {'failed', 'cancelled', 'expired', 'rejected', 'obsolete', 'completed', 'verified'} or
            _cleanup_hold(store, 'legal-hold:' + run_id) or
            _cleanup_hold(store, 'legal-hold-project:' + run['project']) or
            store.one("SELECT 1 FROM tasks WHERE project=? AND source='system_publish' AND body=? "
                      "AND state IN ('queued','planning','executing','evaluating','waiting_budget')",
                      (run['project'], run_id))):
        raise ValueError("Run is active or protected from cleanup")
    if not run_id or Path(run_id).parts != (run_id,) or run_id in ('.', '..'):
        raise ValueError("Unsafe cleanup run identifier")
    directory = store.root / 'runs' / run_id
    if (not run['path'] or Path(run['path']) != directory or
            any(path.is_symlink() for path in (directory, *directory.parents))):
        raise ValueError("Unsafe cleanup run path")
    objects = []
    for name in CLEANUP_NAMES:
        target = directory / name
        if target.is_symlink():
            raise ValueError("Unsafe cleanup artifact symlink")
        if target.exists():
            info = target.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Cleanup artifact must be a directory")
            objects.append({'name': name, 'device': info.st_dev, 'inode': info.st_ino,
                            'modified_ns': info.st_mtime_ns})
    return {'run': run_id, 'objects': objects}


def _cleanup_hold(store, key):
    row = store.one('SELECT value FROM settings WHERE key=?', (key,))
    if row is None:
        return False
    try:
        return json.loads(row['value']) is not False
    except (ValueError, TypeError):
        return True


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_cleanup_receipt(folder, receipt):
    """Persist intent before any rename, independently of the SQLite transaction."""
    temporary = folder / ('.receipt-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / 'receipt.json')
        _sync_directory(folder)
        _sync_directory(folder.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_paths(store, receipt):
    identifier, run_id = receipt['id'], receipt['run']
    if (not isinstance(identifier, str) or len(identifier) != 32 or
            any(c not in '0123456789abcdef' for c in identifier) or
            not isinstance(run_id, str) or not run_id or
            Path(run_id).parts != (run_id,) or run_id in ('.', '..')):
        raise ValueError('Invalid cleanup receipt')
    directory = store.root / 'runs' / run_id
    folder = store.root / 'recycle' / identifier
    if any(p.is_symlink() for p in (directory, *directory.parents, folder, folder.parent)):
        raise ValueError('Unsafe cleanup recovery path')
    names = [item['name'] for item in receipt['objects']]
    if len(set(names)) != len(names) or any(name not in CLEANUP_NAMES for name in names):
        raise ValueError('Invalid cleanup receipt object')
    return directory, folder


def _rename_cleanup(source, target):
    """Use the OS no-replace primitive, including for empty destination directories."""
    import ctypes
    import sys
    if sys.platform in ('darwin', 'linux'):
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == 'darwin':
            result = library.renamex_np(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
        else:
            result = library.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        if result:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    elif os.name == 'nt':
        os.rename(source, target)  # Windows rename never replaces an existing destination.
    else:
        raise OSError('Atomic no-replace cleanup rename unavailable')
    _sync_directory(source.parent)
    _sync_directory(target.parent)


def _place_cleanup(store, receipt, recycled):
    directory, folder = _cleanup_paths(store, receipt)
    source_root, target_root = (directory, folder) if recycled else (folder, directory)
    for item in receipt['objects']:
        source, target = source_root / item['name'], target_root / item['name']
        def matches(path):
            if path.is_symlink() or not path.is_dir():
                return False
            info = path.stat()
            return (info.st_dev, info.st_ino) == (item['device'], item['inode'])
        if matches(target):
            continue
        if target.exists() or target.is_symlink() or not matches(source):
            raise ValueError('Cleanup recovery conflicts with current artifacts; receipt retained')
        _rename_cleanup(source, target)
    _sync_directory(source_root)
    _sync_directory(target_root)
    if not recycled:
        (folder / 'receipt.json').unlink()
        _sync_directory(folder)
        # Unexpected files are preserved, even when the operation is complete.
        if not any(folder.iterdir()):
            folder.rmdir()
        _sync_directory(folder.parent)


def _recover_cleanup_locked(store):
    recycle = store.root / 'recycle'
    if not recycle.exists():
        return
    # A fresh connection avoids stale snapshots and observes the actual commit outcome.
    db = sqlite3.connect(str(store.path), timeout=15)
    try:
        db.execute('BEGIN IMMEDIATE')
        for folder in sorted(recycle.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            journal = folder / 'receipt.json'
            if journal.is_symlink():
                raise ValueError('Unsafe cleanup disk receipt')
            if not journal.exists():
                continue
            receipt = json.loads(journal.read_text(encoding='utf-8'))
            if receipt['id'] != folder.name:
                raise ValueError('Cleanup disk receipt identifier mismatch')
            row = db.execute('SELECT value FROM settings WHERE key=?',
                             ('recycle:' + receipt['id'],)).fetchone()
            if row and json.loads(row[0]) != receipt:
                raise ValueError('Cleanup disk and database receipts disagree')
            _place_cleanup(store, receipt, recycled=bool(row))
    finally:
        db.rollback()
        db.close()


def _cleanup_lock(store):
    import contextlib
    import fcntl
    @contextlib.contextmanager
    def locked():
        recycle = store.root / 'recycle'
        if any(p.is_symlink() for p in (recycle, *recycle.parents)):
            raise ValueError('Unsafe recycle directory')
        recycle.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(recycle.parent)
        descriptor = os.open(recycle / '.cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)
    return locked()


def recover_cleanup(store):
    """Reconcile interrupted cleanup/undo from durable receipts after restart.

    Database unavailability or path conflicts leave the disk receipt intact for retry.
    Expiry never discards the only copy of an artifact.
    """
    with _cleanup_lock(store):
        _recover_cleanup_locked(store)


def recycle_cleanup(store, preview):
    """Move finished artifacts with durable intent and commit-outcome reconciliation."""
    with _cleanup_lock(store):
        _recover_cleanup_locked(store)
        try:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                with store.snapshot():
                    current = preview_cleanup(store, preview['run'])
                if current != preview:
                    raise ValueError('Cleanup preview changed; review again')
                identifier = uuid.uuid4().hex
                folder = store.root / 'recycle' / identifier
                folder.mkdir(mode=0o700)
                receipt = {**current, 'id': identifier, 'expires': time.time() + 7 * 86400}
                _write_cleanup_receipt(folder, receipt)
                _place_cleanup(store, receipt, recycled=True)
                db.execute('INSERT INTO settings VALUES (?,?)',
                           ('recycle:' + identifier, json.dumps(receipt)))
        except BaseException as error:
            # Includes __exit__/commit failure, whose durable outcome may be unknown.
            try:
                _recover_cleanup_locked(store)
            except BaseException as recovery_error:
                raise recovery_error from error
            raise
    return receipt


def undo_cleanup(store, identifier):
    """Restore a live receipt without overwriting newly generated artifacts."""
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
        raise ValueError('Invalid cleanup receipt')
    with _cleanup_lock(store):
        _recover_cleanup_locked(store)
        try:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT value FROM settings WHERE key=?', ('recycle:' + identifier,)).fetchone()
                receipt = json.loads(row['value']) if row else None
                if not receipt or receipt['expires'] <= time.time():
                    raise ValueError('Cleanup receipt missing or expired')
                if receipt['id'] != identifier:
                    raise ValueError('Invalid cleanup receipt identifier')
                directory, folder = _cleanup_paths(store, receipt)
                for item in receipt['objects']:
                    source = folder / item['name']
                    if (source.is_symlink() or not source.is_dir() or
                            (source.stat().st_dev, source.stat().st_ino) != (item['device'], item['inode']) or
                            (directory / item['name']).exists() or (directory / item['name']).is_symlink()):
                        raise ValueError('Undo conflicts with current artifacts')
                # Upgrade receipts made before disk journaling was introduced.
                _write_cleanup_receipt(folder, receipt)
                for item in receipt['objects']:
                    _rename_cleanup(folder / item['name'], directory / item['name'])
                db.execute('DELETE FROM settings WHERE key=?', ('recycle:' + identifier,))
        except BaseException as error:
            try:
                _recover_cleanup_locked(store)
            except BaseException as recovery_error:
                raise recovery_error from error
            raise
        _recover_cleanup_locked(store)
    return {'restored': [item['name'] for item in receipt['objects']], 'run': receipt['run']}


class Maintenance:
    def maintenance_async(self):
        # maintenance 里的批量删除可能耗时数秒，放到后台线程执行，避免阻塞调度循环；
        # 同一时刻只允许一个清理线程，重入直接跳过，未完成的清理由下一轮调度继续。
        # 线程为 daemon，退出时不残留非守护线程；close() 会等待其收尾。
        if time.time() - self.store.setting("maintenance-last", 0) < 3600:
            return
        if not self.maintenance_lock.acquire(blocking=False):
            return
        self.maintenance_thread = threading.Thread(target=self._maintenance_worker,
                                                   name="rsi-maintenance", daemon=True)
        try:
            self.maintenance_thread.start()
        except BaseException:
            self.maintenance_lock.release()
            raise

    def _maintenance_worker(self):
        try:
            with self.scheduler_scope("maintenance"):
                self.maintenance()
        finally:
            self.maintenance_lock.release()

    def maintenance(self):
        import shutil
        if (self.store.root / 'recycle').exists():
            recover_cleanup(self.store)
        now = time.time()
        if now - self.store.setting("maintenance-last", 0) < 3600:
            return
        with self.scheduler_scope("credential_cleanup"):
            self.model_center.cleanup_credentials()
        for project in self.store.query("SELECT * FROM projects"):
            if getattr(self, "maintenance_stop", None) and self.maintenance_stop.is_set():
                return
            with self.scheduler_scope("maintenance_project", project["id"]):
                config = self.config(project["id"])
                cutoff = now - config["limits"]["retention_days"] * 86400
                for run in self.store.query("SELECT id,path,state,task,result FROM runs WHERE project=? AND ended<? AND path IS NOT NULL", (project["id"], cutoff)):
                    if getattr(self, "maintenance_stop", None) and self.maintenance_stop.is_set():
                        return
                    with self.scheduler_scope("maintenance_run", project["id"], run["id"]):
                        with self.dispatch_lock:
                            run = self.store.one("SELECT id,path,state,task,result FROM runs WHERE id=? AND ended<?",
                                                 (run["id"], cutoff))
                            if not run or not run["path"]:
                                continue
                            if (run["state"] == "pr_open" or run["state"] == "merged" and not json.loads(run["result"]).get("monitoring_completed") or
                                    self.store.one("SELECT 1 FROM tasks WHERE project=? AND source='system_publish' AND body=? "
                                                   "AND state IN ('queued','planning','executing','evaluating','waiting_budget')",
                                                   (project["id"], run["id"]))):
                                continue
                            directory = Path(run["path"])
                            runs_root = self.store.root / "runs"
                            relative = directory.relative_to(runs_root)
                            # 只清理该运行的直属目录；词法包含检查不能阻止 .. 或祖先符号链接。
                            if (relative.parts != (run["id"],) or run["id"] in (".", "..") or
                                    any(path.is_symlink() for path in (directory, *directory.parents))):
                                raise ValueError("Unsafe retention run path")
                            directory.resolve().relative_to(runs_root.resolve())
                            if run["state"] == "awaiting_approval":
                                with self.store.connect() as db:
                                    db.execute("BEGIN IMMEDIATE")
                                    db.execute("UPDATE runs SET state='expired' WHERE id=?", (run["id"],))
                                    db.execute("UPDATE tasks SET state='expired' WHERE id=? AND state='awaiting_approval'", (run["task"],))
                        # Publication admission is now closed for this expired candidate.
                        # Filesystem deletion does not hold the scheduler/configuration lock.
                        for name in ["workspace", "candidate", "baseline", "evaluation", "experiments", "dependencies", "acceptance-design", "feedback"]:
                            target = directory / name
                            if target.is_dir() and not target.is_symlink():
                                shutil.rmtree(target)
                # Freeze promotion/rollback while choosing and deleting retired candidates.
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    archive = db.execute("SELECT id FROM candidates WHERE project=? AND status NOT IN ('stable','evaluating') ORDER BY created DESC", (project["id"],)).fetchall()
                    referenced = {r["id"] for r in db.execute(
                        "WITH RECURSIVE ancestors(id,parent) AS ("
                        "SELECT id,parent FROM candidates WHERE project=? AND status='stable' UNION "
                        "SELECT c.id,c.parent FROM candidates c JOIN ancestors a ON c.id=a.parent) SELECT id FROM ancestors",
                        (project["id"],))}
                    for candidate in archive[config["evolution"]["archive_limit"]:]:
                        if candidate["id"] not in referenced:
                            db.execute("DELETE FROM candidates WHERE id=? AND status NOT IN ('stable','evaluating')", (candidate["id"],))
        # 数据保留：清理过期技能、已结束运行的派生数据及其 settings 键；
        # 每类带 LIMIT，首次升级后由后续周期逐步追平历史积压。
        stale = now - 90 * 86400
        if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"):
            self.store.execute("DELETE FROM operations WHERE id IN (SELECT id FROM operations WHERE ended<? "
                               "AND state IN ('completed','failed','cancelled') LIMIT 500)", (stale,))
        for row in self.store.query("SELECT key FROM settings WHERE key LIKE 'batch:%' AND json_valid(value) "
                                    "AND json_extract(value,'$.complete')=1 AND json_extract(value,'$.created')<? LIMIT 100", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=? OR (key>=? AND key<?)",
                               (row["key"], row["key"] + ":", row["key"] + ";"))
        self.store.execute("DELETE FROM settings WHERE key LIKE 'provider-account-health:%' AND json_valid(value) "
                           "AND json_extract(value,'$.retry_at')<?", (now - 7 * 86400,))
        self.store.execute("DELETE FROM skills WHERE expires<?", (now - 30 * 86400,))
        self.store.execute("DELETE FROM request_log WHERE started<?", (now - 7 * 86400,))
        self.store.execute("DELETE FROM phase_times WHERE id IN (SELECT id FROM phase_times WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM remote_waits WHERE id IN (SELECT id FROM remote_waits WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM task_revisions WHERE id IN (SELECT id FROM task_revisions WHERE created<? AND task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state NOT IN ('queued','planning','executing','evaluating')) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM submissions WHERE rowid IN (SELECT rowid FROM submissions WHERE task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state IN ('merged','obsolete','cancelled','expired','reverted','superseded','pr_closed')) LIMIT 500)", (stale,))
        # 交付生命周期未结束的运行（PR 跟踪、合并后监控）仍需要这些键；
        # 与上方目录清理的保护范围保持一致，不做无条件删除。
        # 已清理的运行不能继续占据 LIMIT，否则后续周期永远无法排空积压。
        prefixes = ("run-baseline:", "automation:", "delivery:")
        # 独立的精确键/冒号前缀范围查询可使用 settings 主键索引，避免逐运行全表扫描。
        # ':' 的后继字符是 ';'，二进制排序范围只覆盖该运行的冒号后缀键。
        remaining = " OR ".join(
            "EXISTS (SELECT 1 FROM settings s WHERE s.key='" + prefix + "'||r.id) OR "
            "EXISTS (SELECT 1 FROM settings s WHERE s.key>='" + prefix + "'||r.id||':' "
            "AND s.key<'" + prefix + "'||r.id||';')"
            for prefix in prefixes)
        for run in self.store.query(
                "SELECT r.id FROM runs r WHERE r.ended IS NOT NULL AND r.ended<? AND r.state NOT IN "
                "('pr_open','merged','unverified_merge','releasing','deploying','pr_closed') "
                "AND NOT (r.state='superseded' AND CASE WHEN json_valid(r.result) "
                "THEN coalesce(json_extract(r.result,'$.close_pending'),0) ELSE 1 END=1) AND (" +
                remaining + ") LIMIT 200", (stale,)):
            for prefix in prefixes:
                key = prefix + run["id"]
                self.store.execute("DELETE FROM settings WHERE key=? OR (key>=? AND key<?)",
                                   (key, key + ":", key + ";"))
        # 批量初筛决定已直接写入 task_decisions 并随任务持久；这里只清理
        # 升级前遗留的 triage-state 键（存量暂缓任务会在 reassess 中迁移）。
        for task in self.store.query("SELECT id FROM tasks WHERE state<>'deferred' AND updated<? AND EXISTS "
                                     "(SELECT 1 FROM settings WHERE key='triage-state:'||tasks.id) LIMIT 200", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", ("triage-state:" + task["id"],))
        # 过滤在 LIMIT 之前完成：新鲜条目占满一批时，过期条目仍能在本轮被删除。
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'task-proof:%' AND "
                "CASE WHEN json_valid(value) THEN (json_extract(value,'$.at') IS NULL OR json_extract(value,'$.at')<?) ELSE 0 END LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'provider-health:%' AND "
                "CASE WHEN json_valid(value) THEN (json_extract(value,'$.at') IS NULL OR (json_extract(value,'$.paused')=0 AND json_extract(value,'$.at')<?)) ELSE 0 END LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        # Issue 评论缓存按 issue 数量累积（每个键含最多 20 条评论正文）；
        # 超过保留期未刷新的键不会再被命中（处理时总会刷新），直接删除。
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'issue-comments:%' AND "
                "CASE WHEN json_valid(value) THEN (json_extract(value,'$.checked_at') IS NULL OR json_extract(value,'$.checked_at')<?) ELSE 0 END LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        # Retire stale connection-signature directories after proving they are unused.
        with self.dispatch_lock:
            keys = set()
            configs_valid = True
            for scope in [None] + [row["id"] for row in self.store.query("SELECT id FROM projects")]:
                try:
                    config = self.config(scope)
                except (ValueError, TypeError, KeyError):
                    configs_valid = False
                    break
                from .config import BUILTIN_PROVIDERS
                keys.update(self.model_center.key(provider, scope) for provider in [*BUILTIN_PROVIDERS, *config["providers"]])
            # Frozen runs can still refer to a retired connection signature.
            active = self.store.one("SELECT 1 FROM runs WHERE ended IS NULL LIMIT 1")
            if configs_valid and not active:
                for row in self.store.query("SELECT key,value FROM settings WHERE key LIKE 'model-center:%' "
                                            "AND json_valid(value) AND json_extract(value,'$.fetched')<? "
                                            "AND key NOT IN (SELECT value FROM json_each(?)) LIMIT 4000",
                                            (stale, json.dumps(sorted(keys)))):
                    try:
                        fetched = json.loads(row["value"]).get("fetched")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if row["key"] not in keys and fetched and fetched < stale:
                        self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        cutoff_event = self.store.one("SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET 1000")
        if cutoff_event:
            self.store.execute("DELETE FROM events WHERE created<? AND seq<?", (now - 90 * 86400, cutoff_event["seq"]))
        # Passive checkpoint yields to readers; archive checks are capped per cycle.
        self.store.wal_diagnostics(checkpoint=True)
        from .storage_tools import verify_archive
        for path in sorted((self.store.root / "archives").glob("*.sqlite3.gz")):
            if path.is_symlink():
                continue
            key = "archive-verification:" + path.name
            previous = self.store.setting(key, {})
            if isinstance(previous, dict) and now - previous.get("checked_at", 0) < 7 * 86400:
                continue
            try:
                report = verify_archive(path, timeout=5)
            except (OSError, ValueError, TimeoutError, sqlite3.Error, EOFError, zlib.error) as exc:
                report = {"valid": False, "checked_at": now, "error_type": type(exc).__name__}
            self.store.put_setting(key, report)
            break
        self.store.put_setting("maintenance-last", now)
