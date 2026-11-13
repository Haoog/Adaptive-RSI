"""Background artifact retention and bounded state maintenance."""
import json
import logging
import math
import shutil
import sqlite3
import threading
import time
import zlib
from pathlib import Path
import os
import stat


CLEANUP_NAMES = ("workspace", "candidate", "baseline", "evaluation", "experiments",
                 "dependencies", "acceptance-design", "feedback")

# 维护默认每小时一轮；失败后的最短重试间隔按次数指数增长，上限一小时。
MAINTENANCE_INTERVAL = 3600
MAINTENANCE_RETRY_SECONDS = 300
MAINTENANCE_RETRY_MAX_SECONDS = 3600

# 确定性拒绝标记的重验窗口：运行目录是不是符号链接取决于文件系统状态，
# 运维把链接换成同名真实目录后 runs.path 并不变化，永久跳过会饿死该运行；
# 超过窗口的标记重新进入候选，仍不安全则重写标记顺延，恢复正常则清除。
RETENTION_REJECT_REVALIDATE_SECONDS = 6 * 3600

# 归档校验报告的有效期：过期后才重新解压验证，每轮最多验证一个归档。
ARCHIVE_VERIFICATION_SECONDS = 7 * 86400
ARCHIVE_VERIFICATION_READ_BATCH_SIZE = 128

# 保留删除只把页移入 freelist；库以 auto_vacuum=0 创建，不 VACUUM 文件永不缩小。
# 空闲页达到四分之一且磁盘留足重写整库的余量（VACUUM 峰值约需一份库文件）时
# 才回收，避免为少量空闲页反复重写整个数据库。
RECLAIM_FREELIST_RATIO = 0.25
RECLAIM_DISK_HEADROOM = 2.5


def _vacuum(store):
    with store.connect() as db:
        db.execute("VACUUM")


def _reclaim_database(store, dispatch_lock):
    """VACUUM 回收保留删除留下的空闲页；失败只留证据，不影响本轮清理。

    备份与归档按库文件大小判定磁盘预算，文件虚胖会让它们在磁盘仍有余量时
    被拒。空闲页占比、磁盘余量和活动运行都满足才回收：与调度共用锁复核
    「无活动运行」，避免放行新运行后 VACUUM 持写锁把其写入拖到超时。
    """
    try:
        with store.connect() as db:
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
        before = store.path.stat().st_size
        headroom = shutil.disk_usage(store.path.parent).free
    except (OSError, sqlite3.Error):
        logging.getLogger(__name__).exception("Database reclaim precheck failed")
        return
    if not pages or free_pages / pages < RECLAIM_FREELIST_RATIO:
        return
    if headroom < RECLAIM_DISK_HEADROOM * before:
        return
    with dispatch_lock:
        if store.one("SELECT 1 FROM runs WHERE ended IS NULL LIMIT 1"):
            return
        try:
            _vacuum(store)
        except sqlite3.Error as exc:
            message = "%s: %s" % (type(exc).__name__, exc)
            logging.getLogger(__name__).warning("Database reclaim skipped (%s)", message)
            try:
                store.event("database_reclaim_failed", message,
                            data={"error_type": type(exc).__name__, "page_count": pages,
                                  "freelist_count": free_pages})
            except Exception:
                logging.getLogger(__name__).exception("Reclaim failure persistence failed")
            return
    try:
        after = store.path.stat().st_size
    except OSError:
        after = 0
    try:
        store.event("database_reclaimed",
                    "Reclaimed %d bytes from the state database" % max(0, before - after),
                    data={"before_bytes": before, "after_bytes": after,
                          "page_count": pages, "freelist_count": free_pages})
    except Exception:
        logging.getLogger(__name__).exception("Reclaim event persistence failed")


# 保留删除只改行数不改结构；规划器只有 ANALYZE 之后才看得到真实基数。
# SQLite 3.46.0 才让 PRAGMA optimize 自动分析没有 sqlite_stat1 记录的表，并支持
# 0x10000 位按全表体积判断是否需要重分析；更早的版本会静默跳过本连接没查询过的
# 表，而 Python 3.9+ 可以链接任意 SQLite 版本，所以旧版本补一次有界显式 ANALYZE。
PLANNER_STAT_TABLES = ("events",)
PLANNER_OPTIMIZE_PRAGMA = "PRAGMA optimize=0x10002"
PLANNER_OPTIMIZE_ALL_TABLES = sqlite3.sqlite_version_info >= (3, 46, 0)


def _analyze_tables(db, tables):
    """ANALYZE ``tables`` in full so real index cardinalities reach the planner.

    ``PRAGMA analysis_limit`` would sample instead of scanning, and sampled
    ``sqlite_stat1`` rows collapse the two candidate indexes of the events
    lookup into the same per-prefix count (120k rows report ~1000 per ``kind``
    and per ``project``), so the planner keeps ``project_events`` plus a
    ``TEMP B-TREE`` for ``GROUP BY kind`` instead of scanning the covered
    ``event_kind_created``.  Only the bounded maintenance tables are analyzed
    here and a full pass is cheap (120k rows in well under a second).
    """
    # 表名来自上面的常量元组，不拼接外部输入。
    for table in tables:
        db.execute("ANALYZE %s" % table)


def refresh_query_planner(store):
    """Refresh sqlite_stat1 after retention so plans follow real cardinalities.

    ``PRAGMA optimize=0x10002`` is SQLite's documented periodic form for
    long-lived connections, but only from 3.46.0 does it check every table's
    size (bit 0x10000) and auto-analyze tables without ``sqlite_stat1`` rows.
    Older builds silently skip tables the connection never queried, and Python
    3.9+ can link any SQLite release, so those builds fall back to an explicit
    bounded ``ANALYZE``.  It runs on a borrowed connection with no explicit
    transaction, so the write lock ANALYZE briefly takes belongs to it alone.
    Failures are logged and never abort the retention round.
    """
    try:
        with store.connect() as db:
            db.execute(PLANNER_OPTIMIZE_PRAGMA).fetchall()
            if not PLANNER_OPTIMIZE_ALL_TABLES:
                _analyze_tables(db, PLANNER_STAT_TABLES)
    except sqlite3.Error:
        logging.getLogger(__name__).warning("Query planner statistics refresh failed",
                                            exc_info=True)
        return False
    return True


def _retry_deadline(value):
    """Read the earliest allowed retry from the persisted backoff record."""
    if not isinstance(value, dict):
        return 0
    try:
        return float(value.get("retry_at", 0))
    except (TypeError, ValueError):
        return 0


def _cleanup_hold(store, key):
    row = store.one('SELECT value FROM settings WHERE key=?', (key,))
    if row is None:
        return False
    try:
        return json.loads(row['value']) is not False
    except (ValueError, TypeError):
        return True


def _merged_monitoring_completed(result):
    """Report whether a merged run finished post-merge monitoring.

    Mirrors _cleanup_hold's fail-closed rule: an unreadable result keeps the
    run retained like an unfinished monitor, and a corrupt value must never
    count as completed evidence.  Readable dicts keep the previous truthiness
    judgment; only previously crashing shapes (non-JSON, non-objects) change,
    from a per-run scheduler failure to an ordinary retained skip.
    """
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("monitoring_completed"))


_ARCHIVE_VERIFICATION_MISSING = object()


def _archive_verification_due(value, now):
    """Return (due, previous_type) for one stored archive verification report.

    Settings are untrusted input: a restored backup or a hand edit can leave a
    string, list or NaN under ``checked_at``. The missing sentinel distinguishes
    an absent row from a present SQL NULL; absent rows have no previous shape,
    while SQL NULL is unreadable. Unusable records count as expired so the
    archive is verified again and the report overwritten; the second item names
    the original shape (never its content) for the repair event, and is None
    when no record existed or the stored one is usable.
    """
    if value is _ARCHIVE_VERIFICATION_MISSING:
        return True, None
    try:
        previous = json.loads(value)
    except (ValueError, TypeError):
        return True, "unreadable"
    checked_at = previous.get("checked_at") if isinstance(previous, dict) else None
    if (type(checked_at) not in (int, float) or not math.isfinite(checked_at) or
            checked_at <= 0):
        return True, type(previous).__name__
    return now - checked_at >= ARCHIVE_VERIFICATION_SECONDS, None


def retention_hold(store, run_id, project):
    """Return the legal hold that protects a run from cleanup, or None.

    Every removal entry point uses this check.  An unreadable value counts as a
    hold, so a corrupt setting can never unlock deletion of retained evidence.
    """
    for key in ('legal-hold:' + run_id, 'legal-hold-project:' + project):
        if _cleanup_hold(store, key):
            return key
    return None


def _legal_hold_guard(alias):
    """SQL condition excluding runs protected by a run- or project-level hold.

    Mirrors retention_hold's fail-closed reading: only an explicit JSON false
    releases a hold, so a corrupt or non-boolean value still keeps the run's
    derived evidence keys.  ``alias`` is the runs table alias of the enclosing
    eligibility query.
    """
    return ("AND NOT EXISTS (SELECT 1 FROM settings AS held WHERE "
            "held.key IN ('legal-hold:'||%s.id, 'legal-hold-project:'||%s.project) "
            "AND CASE WHEN json_valid(held.value) THEN json_type(held.value)='false' "
            "ELSE 0 END=0) " % (alias, alias))


def _remove_owned_tree(parent, name):
    """Remove one entry beneath an open directory without following any link.

    Dependency copies are sealed read-only by environment.prepare_cached, so the
    directory owning each entry must be made writable before it can be emptied.
    Directory descriptors pin the verified tree: a link swapped in later fails
    O_NOFOLLOW, and unlink/rmdir always act on the recorded parent entry.

    The walk is iterative rather than recursive: dependency trees routinely nest
    deeper than sys.getrecursionlimit() (node_modules-style paths), where a
    recursive walk dies on RecursionError and the run can never be cleaned.
    Only the deepest directory stays open, so depth is also unbounded by the
    process descriptor limit.  Ascending re-opens ".." and verifies the recorded
    (device, inode); a parent moved out from under the walk aborts the removal
    instead of deleting outside the verified tree.
    """
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    # Frames: [entry name in its parent, device, inode, pending child names].
    # device/inode and the child list are filled when the directory is scanned.
    stack = [[name, 0, 0, None]]
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        while stack:
            frame = stack[-1]
            if frame[3] is None:
                dirstat = os.fstat(descriptor)
                frame[1], frame[2] = dirstat.st_dev, dirstat.st_ino
                mode = stat.S_IMODE(dirstat.st_mode)
                if mode & stat.S_IRWXU != stat.S_IRWXU:
                    # Only directories inside the artifact being removed regain
                    # write access.
                    os.fchmod(descriptor, mode | stat.S_IRWXU)
                with os.scandir(descriptor) as entries:
                    frame[3] = [entry.name for entry in entries]
            pending = frame[3]
            descended = False
            while pending:
                child = pending.pop()
                try:
                    cinfo = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(cinfo.st_mode):
                    # Files and links alike: the entry itself is unlinked and a
                    # link target outside the artifact is never followed.
                    os.unlink(child, dir_fd=descriptor)
                    continue
                deeper = os.open(child, flags, dir_fd=descriptor)
                stack.append([child, 0, 0, None])
                os.close(descriptor)
                descriptor = deeper
                descended = True
                break
            if descended:
                continue
            stack.pop()
            if not stack:
                os.close(descriptor)
                descriptor = None
                os.rmdir(frame[0], dir_fd=parent)
            else:
                upper = os.open('..', flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = upper
                pstat = os.fstat(descriptor)
                if (pstat.st_dev, pstat.st_ino) != (stack[-1][1], stack[-1][2]):
                    raise NotADirectoryError('artifact tree changed during removal')
                os.rmdir(frame[0], dir_fd=descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _remove_artifact(directory, name):
    """Delete a finished run's own artifact, including read-only dependency copies.

    The caller already proved the run directory is the controller-owned path; a
    vanished artifact means a concurrent pass finished first and stays silent.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                             getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return
    try:
        _remove_owned_tree(descriptor, name)
    finally:
        os.close(descriptor)


def _held_in_transaction(db, run_id, project):
    """Read a run's legal holds on the connection that owns the write transaction.

    Reading through the deleting connection keeps the check inside the same
    serialisation domain as the deletion: a hold committed before BEGIN IMMEDIATE
    is observed here, while one written concurrently cannot commit until this
    transaction ends.  An unreadable value counts as a hold, exactly like
    retention_hold, so a corrupt setting can never unlock retained evidence.
    """
    for key in ('legal-hold:' + run_id, 'legal-hold-project:' + project):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if row is None:
            continue
        try:
            if json.loads(row[0]) is not False:
                return key
        except (ValueError, TypeError):
            return key
    return None


def _remove_run_artifacts(store, run_id, project, directory, stored=None):
    """Delete a finished run's own artifacts, one write transaction per artifact.

    Re-reading the legal hold under BEGIN IMMEDIATE serialises the check with
    the unlink/rmdir it authorises: a hold committed first stops the remaining
    deletions, and a hold written during a deletion waits for that transaction
    to finish.  Per-artifact transactions bound the write-lock span to a single
    artifact instead of the whole multi-project cleanup pass.  Once no artifacts
    remain, one final transaction detaches the run's path so later passes filter
    it out through `path IS NOT NULL` instead of re-scanning it forever.
    ``stored`` is the exact runs.path value from the database, which may differ
    from the resolved ``directory`` by a symlink layer (e.g. /var on macOS).
    """
    for name in CLEANUP_NAMES:
        target = directory / name
        if target.is_symlink() or not target.is_dir():
            continue
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if _held_in_transaction(db, run_id, project):
                return False
            # Re-check under the write lock: an entry swapped for a link after
            # the caller's lexical check must not be removed as the artifact.
            if target.is_symlink() or not target.is_dir():
                continue
            _remove_artifact(directory, name)
    # All own artifacts are gone (deleted here or already absent). Detach the
    # run from its path so later passes filter it out via `path IS NOT NULL`
    # instead of re-opening a write transaction and re-stat'ing an empty
    # directory for every expired run on every maintenance cycle.  The hold is
    # re-read inline so a hold committed during the deletions keeps the path.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _held_in_transaction(db, run_id, project):
            return False
        # Match the exact path that was cleaned so a concurrently rewritten
        # path is never detached from artifacts that still exist.
        db.execute('UPDATE runs SET path=NULL WHERE id=? AND path IN (?,?)',
                   (run_id, str(directory), stored if stored is not None else str(directory)))
    return True


class Maintenance:
    # 上一次维护失败后的进程内退避截止时间；数据库不可写时仍能拦住每秒重试。
    maintenance_retry_at = 0

    def maintenance_async(self):
        # maintenance 里的批量删除可能耗时数秒，放到后台线程执行，避免阻塞调度循环；
        # 同一时刻只允许一个清理线程，重入直接跳过，未完成的清理由下一轮调度继续。
        # 线程为 daemon，退出时不残留非守护线程；close() 会等待其收尾。
        now = time.time()
        if now - self.store.setting("maintenance-last", 0) < MAINTENANCE_INTERVAL:
            return
        # 崩溃的清理线程不更新 maintenance-last；若不按退避拦截，调度循环会在
        # 下一个 tick（约 1 秒）重新拉起一个必然失败的新线程。
        if now < max(self.maintenance_retry_at,
                     _retry_deadline(self.store.setting("maintenance-retry", {}))):
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
                try:
                    self.maintenance()
                except Exception as exc:
                    self._record_maintenance_failure(exc)
                    raise
                self._clear_maintenance_failure()
        finally:
            self.maintenance_lock.release()

    def _record_maintenance_failure(self, exc):
        """Record a failed pass and defer the next attempt instead of retrying each tick."""
        try:
            previous = self.store.setting("maintenance-retry", {})
        except Exception:
            previous = {}
        failures = previous.get("failures", 0) if isinstance(previous, dict) else 0
        if type(failures) is not int or failures < 0:
            failures = 0
        failures += 1
        delay = min(MAINTENANCE_RETRY_SECONDS * 2 ** min(failures - 1, 4),
                    MAINTENANCE_RETRY_MAX_SECONDS)
        now = time.time()
        # 先落到进程内：磁盘满或数据库不可写时，事件与退避记录本身也写不进去。
        self.maintenance_retry_at = now + delay
        message = "%s: %s" % (type(exc).__name__, exc)
        logging.getLogger(__name__).warning("Maintenance failed (%s); retrying in %ss",
                                            message, delay)
        try:
            self.store.event("maintenance_failed", message,
                             data={"error_type": type(exc).__name__, "failures": failures,
                                   "retry_at": now + delay, "retry_in": delay})
            self.store.put_setting("maintenance-retry",
                                   {"retry_at": now + delay, "failures": failures,
                                    "error_type": type(exc).__name__, "message": message[:500]})
        except Exception:
            logging.getLogger(__name__).exception("Maintenance failure persistence failed")

    def _reject_retention_path(self, run, project, directory, reason):
        """Mark a deterministically rejected runs.path so later passes skip it.

        标记按 (run.id, run.path) 匹配：运维把 runs.path 修回 runs/ 内后该
        运行自动回到候选。符号链接这类文件系统条件可在 path 不变时变化，
        因此标记只屏蔽 RETENTION_REJECT_REVALIDATE_SECONDS 一个窗口，到期
        重新检查；写 settings 失败时异常冒泡到 scheduler_scope，下个周期
        仍会按原路径重试，不会留下只有一半的证据。
        """
        self.store.put_setting("retention_path_rejected:" + run["id"],
                               {"path": run["path"], "reason": reason,
                                "project": project, "rejected": time.time()})
        try:
            self.store.event("retention_path_rejected",
                             "Rejected unsafe retention path for %s: %s" % (run["id"], reason),
                             project, run["id"], {"path": str(directory)})
        except Exception:
            logging.getLogger(__name__).exception(
                "Retention rejection event persistence failed: %s", run["id"])

    def _clear_maintenance_failure(self):
        """Reset the backoff after a healthy pass; a stale record is harmless once past."""
        self.maintenance_retry_at = 0
        try:
            self.store.execute("DELETE FROM settings WHERE key='maintenance-retry'")
        except Exception:
            logging.getLogger(__name__).exception("Maintenance backoff reset failed")

    def maintenance(self):
        now = time.time()
        if now - self.store.setting("maintenance-last", 0) < MAINTENANCE_INTERVAL:
            return
        with self.scheduler_scope("credential_cleanup"):
            self.model_center.cleanup_credentials()
        for project in self.store.query("SELECT * FROM projects"):
            if getattr(self, "maintenance_stop", None) and self.maintenance_stop.is_set():
                return
            with self.scheduler_scope("maintenance_project", project["id"]):
                config = self.config(project["id"])
                cutoff = now - config["limits"]["retention_days"] * 86400
                revalidate = now - RETENTION_REJECT_REVALIDATE_SECONDS
                # 已确定性拒绝的 runs.path（见 _reject_retention_path）按 (id,path)
                # 匹配跳过；运维修正 path 后标记失配，该运行自动回到清理候选。
                # 符号链接等文件系统条件可在 path 不变时变化，因此只屏蔽一个
                # 重验窗口：超窗标记回到候选，仍不安全则重新标记，恢复则清除。
                for run in self.store.query(
                        "SELECT id,path,state,task,result FROM runs WHERE project=? AND ended<? AND path IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM settings WHERE key='retention_path_rejected:' || runs.id "
                        "AND json_valid(value) AND json_extract(value,'$.path')=runs.path "
                        "AND json_extract(value,'$.rejected')>?)",
                        (project["id"], cutoff, revalidate)):
                    if getattr(self, "maintenance_stop", None) and self.maintenance_stop.is_set():
                        return
                    with self.scheduler_scope("maintenance_run", project["id"], run["id"]):
                        with self.dispatch_lock:
                            run = self.store.one("SELECT id,path,state,task,result FROM runs WHERE id=? AND ended<?",
                                                 (run["id"], cutoff))
                            if not run or not run["path"]:
                                continue
                            # 保留标记与手动清理共享同一判断；损坏值按已保留处理。
                            if retention_hold(self.store, run["id"], project["id"]):
                                continue
                            if (run["state"] == "pr_open" or run["state"] == "merged" and not _merged_monitoring_completed(run["result"]) or
                                    self.store.one("SELECT 1 FROM tasks WHERE project=? AND source='system_publish' AND body=? "
                                                   "AND state IN ('queued','planning','executing','evaluating','waiting_budget')",
                                                   (project["id"], run["id"]))):
                                continue
                            # 存储的 path 与 store.root 可能相差一层 OS 符号链接（如 macOS
                            # /var→/private/var），但 store.root 内部不允许任何符号链接：
                            # runs 目录或运行目录为链接时按越界拒绝，存疑路径保留不修。
                            directory = Path(run["path"])
                            runs_root = self.store.root / "runs"
                            # runs_root 自身是环境故障（或为链接、或 stat 失败），影响全部运行，
                            # 仍走 scheduler_error 周期性重试；目录不可读时的 OSError 同样冒泡。
                            if runs_root.is_symlink():
                                raise ValueError("Unsafe retention run path")
                            # 以下拒绝只由该行数据决定，属确定性拒绝：写运维证据后跳过本运行，
                            # 下个周期由 SQL 预检过滤，不再每小时重复 scheduler_error。
                            if (directory.is_symlink() or
                                    Path(run["id"]).parts != (run["id"],) or run["id"] in (".", "..") or
                                    directory.name != run["id"]):
                                self._reject_retention_path(run, project["id"], directory, "unsafe run path")
                                continue
                            inside = directory.parent.resolve() == runs_root
                            if not inside:
                                self._reject_retention_path(run, project["id"], directory, "outside runs root")
                                continue
                            directory = runs_root / run["id"]
                            # 到达这里说明路径本轮已通过全部文件系统检查：清理同一 path
                            # 的过期拒绝标记，让修复后的运行不再被旧证据挡住。仅删过期
                            # 标记；新鲜标记（刚由本轮或并发轮写入）原样保留。
                            self.store.execute(
                                "DELETE FROM settings WHERE key=? AND json_valid(value) "
                                "AND json_extract(value,'$.path')=? AND json_extract(value,'$.rejected')<=?",
                                ("retention_path_rejected:" + run["id"], run["path"], revalidate))
                            if run["state"] == "awaiting_approval":
                                with self.store.connect() as db:
                                    db.execute("BEGIN IMMEDIATE")
                                    db.execute("UPDATE runs SET state='expired' WHERE id=?", (run["id"],))
                                    db.execute("UPDATE tasks SET state='expired' WHERE id=? AND state='awaiting_approval'", (run["task"],))
                        # Publication admission is now closed for this expired candidate.
                        # 删除仍不持有调度/配置锁，但每个工件在各自的 BEGIN IMMEDIATE 内
                        # 用同一连接重查 hold：预检后提交的 hold 会阻止余下删除，删除期间
                        # 的 hold 写入会等到该事务结束后才提交。
                        _remove_run_artifacts(self.store, run["id"], project["id"], directory,
                                              stored=run["path"])
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
        # relationship_audit 只报告不处置；孤儿行按类 LIMIT 分批隔离，失效
        # task_dependencies 清除后被 p.id IS NULL 挡住的任务恢复可调度。
        self.store.quarantine_orphans()
        if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"):
            self.store.execute("DELETE FROM operations WHERE id IN (SELECT id FROM operations WHERE ended<? "
                               "AND state IN ('completed','failed','cancelled') LIMIT 500)", (stale,))
        for row in self.store.query("SELECT key FROM settings WHERE key LIKE 'batch:%' AND json_valid(value) "
                                    "AND json_extract(value,'$.complete')=1 AND json_extract(value,'$.created')<? LIMIT 100", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=? OR (key>=? AND key<?)",
                               (row["key"], row["key"] + ":", row["key"] + ";"))
        self.store.execute("DELETE FROM settings WHERE key LIKE 'provider-account-health:%' AND json_valid(value) "
                           "AND json_extract(value,'$.retry_at')<?", (now - 7 * 86400,))
        # baseline:<project> 是旧版每次 fetch 覆写的死键，后端已无读取方；写入
        # 已移除，这里按索引前缀分批清掉存量行，避免旧库永久保留。
        self.store.execute("DELETE FROM settings WHERE key IN (SELECT key FROM settings "
                           "WHERE key>=? AND key<? LIMIT 400)", ("baseline:", "baseline;"))
        self.store.execute("DELETE FROM skills WHERE rowid IN (SELECT rowid FROM skills WHERE expires<? LIMIT 500)", (now - 30 * 86400,))
        self.store.execute("DELETE FROM request_log WHERE rowid IN (SELECT rowid FROM request_log WHERE started<? LIMIT 500)", (now - 7 * 86400,))
        self.store.execute("DELETE FROM phase_times WHERE id IN (SELECT id FROM phase_times WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM remote_waits WHERE id IN (SELECT id FROM remote_waits WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM task_revisions WHERE id IN (SELECT id FROM task_revisions WHERE created<? AND task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state NOT IN ('queued','planning','executing','evaluating')) LIMIT 500)", (stale, stale))
        self.store.execute("DELETE FROM submissions WHERE rowid IN (SELECT rowid FROM submissions WHERE task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state IN ('merged','obsolete','cancelled','expired','reverted','superseded','pr_closed')) LIMIT 500)", (stale,))
        # 交付生命周期未结束的运行（PR 跟踪、合并后监控）仍需要这些键；
        # 与上方目录清理的保护范围保持一致，不做无条件删除。运行或项目
        # legal hold 同样保留全部派生证据键（基线、推送、合并意图、交付），
        # 只有显式 JSON false 解除。
        # 已清理的运行不能继续占据 LIMIT，否则后续周期永远无法排空积压。
        prefixes = ("run-baseline:", "automation:", "delivery:", "merge-intent:", "delivery-push:")
        # 在一致读快照中按索引扫描键；最长匹配的运行 ID 拥有该键。
        runs = set()
        with self.store.connect() as db:
            db.execute("BEGIN")
            for prefix in prefixes:
                last = prefix[:-1]
                while len(runs) < 200:
                    rows = db.execute("SELECT key FROM settings WHERE key>=? AND key<? AND key>? ORDER BY key LIMIT 256",
                                      (prefix, prefix[:-1] + ";", last)).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        payload = row["key"][len(prefix):]
                        candidate = payload
                        while True:
                            owner = db.execute(
                                "SELECT id, ended IS NOT NULL AND ended<? AND state NOT IN "
                                "('pr_open','merged','unverified_merge','releasing','deploying','pr_closed') "
                                "AND NOT (state='superseded' AND CASE WHEN json_valid(result) "
                                "THEN coalesce(json_extract(result,'$.close_pending'),0) ELSE 1 END=1) "
                                "AND NOT (state='superseded' AND CASE WHEN json_valid(result) "
                                "THEN coalesce(json_extract(result,'$.pr'),'') ELSE 'x' END='' "
                                "AND CASE WHEN json_valid(result) "
                                "THEN coalesce(json_extract(result,'$.delivery_recovery_abandoned'),0) ELSE 0 END=0 "
                                "AND EXISTS (SELECT 1 FROM settings WHERE key='delivery-push:'||runs.id)) "
                                + _legal_hold_guard("runs") +
                                "AS eligible FROM runs WHERE id=?", (stale, candidate)).fetchone()
                            if owner:
                                if owner["eligible"]:
                                    runs.add(owner["id"])
                                break
                            if ":" not in candidate:
                                break
                            candidate = candidate.rsplit(":", 1)[0]
                        if len(runs) >= 200:
                            break
                    last = rows[-1]["key"]
                if len(runs) >= 200:
                    break
        # 单个有界批次共享事务，避免每个运行的每类键单独提交。
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for run in runs:
                eligible = db.execute(
                    "SELECT 1 FROM runs r WHERE r.id=? AND r.ended<? AND r.state NOT IN "
                    "('pr_open','merged','unverified_merge','releasing','deploying','pr_closed') "
                    "AND NOT (r.state='superseded' AND CASE WHEN json_valid(r.result) "
                    "THEN coalesce(json_extract(r.result,'$.close_pending'),0) ELSE 1 END=1) "
                    "AND NOT (r.state='superseded' AND CASE WHEN json_valid(r.result) "
                    "THEN coalesce(json_extract(r.result,'$.pr'),'') ELSE 'x' END='' "
                    "AND CASE WHEN json_valid(r.result) "
                    "THEN coalesce(json_extract(r.result,'$.delivery_recovery_abandoned'),0) ELSE 0 END=0 "
                    "AND EXISTS (SELECT 1 FROM settings WHERE key='delivery-push:'||r.id)) "
                    + _legal_hold_guard("r"),
                    (run, stale)).fetchone()
                if not eligible:
                    continue
                for prefix in prefixes:
                    key = prefix + run
                    # 嵌套运行 ID 优先拥有其命名空间，父运行清理不能越界。
                    db.execute(
                        "DELETE FROM settings AS s WHERE (s.key=? OR (s.key>=? AND s.key<?)) "
                        "AND NOT EXISTS (SELECT 1 FROM runs child WHERE child.id>=? AND child.id<? "
                        "AND (s.key=?||child.id OR (s.key>=?||child.id||':' AND s.key<?||child.id||';')))",
                        (key, key + ":", key + ";", run + ":", run + ";", prefix, prefix, prefix))
        # pr-feedback:<sha256([project,task])>: 观测键与 handled:<identity> 核销键按
        # 任务前缀索引而非运行，上面的运行级清理永远碰不到它们，只增不减。
        # _supersede_pr 只在任务处于 pr_open 时读 handled:；任务进入终态后无论是否
        # 被重新打开（updated 会刷新而不再陈旧），都不可能再有 supersede 判读，键对
        # 新 PR 也不会命中（观测身份包含 PR URL/运行 ID），同批删除两类键是安全的。
        # 任务前缀是散列值、无法在 SQL 里按 settings 存在性过滤：改为按 id 顺序游标
        # 扫描、每轮最多清理 200 个「实际仍有键」的任务；无键任务不占配额，游标扫完
        # 回到起点，积压（含已清理任务的重复占位）不再卡住后续任务。
        from .delivery import _pr_feedback_prefix
        cursor = self.store.setting('maintenance-pr-feedback-cursor', '') or ''
        cleaned = 0
        exhausted = True
        for task in self.store.stream(
                "SELECT id,project FROM tasks WHERE updated<? AND state IN "
                "('merged','obsolete','cancelled','expired','reverted','superseded','pr_closed') "
                "AND id>? ORDER BY id", (stale, cursor)):
            prefix = _pr_feedback_prefix(task["project"], task["id"])
            if not self.store.one(
                    "SELECT 1 FROM settings WHERE (key>=? AND key<?) OR (key>=? AND key<?) LIMIT 1",
                    (prefix, prefix + "~", "handled:" + prefix, "handled:" + prefix + "~")):
                continue
            self.store.execute(
                "DELETE FROM settings WHERE (key>=? AND key<?) OR (key>=? AND key<?)",
                (prefix, prefix + "~", "handled:" + prefix, "handled:" + prefix + "~"))
            cleaned += 1
            if cleaned >= 200:
                self.store.put_setting('maintenance-pr-feedback-cursor', task["id"])
                exhausted = False
                break
        if exhausted:
            self.store.put_setting('maintenance-pr-feedback-cursor', '')
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
        # case-exposure 预留键按「实验×案例」永久累积；仅在候选已离开 evaluating
        # （归档、删除或落定）且超过保留期后删除。仍在评估中的候选保留键，
        # 保证重试/重复调用时 reserve_case_exposure 的幂等语义不变。
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'case-exposure:%' AND "
                "json_valid(value) AND json_extract(value,'$.created')<? AND "
                "NOT EXISTS (SELECT 1 FROM candidates WHERE "
                "id=json_extract(settings.value,'$.experiment') AND status='evaluating') "
                "LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        # Retire stale connection-signature directories after proving they are unused.
        # 全局 dispatch_lock 只覆盖「配置快照 + 无活跃运行」的判定；候选扫描与批量删除
        # 都在锁外完成，避免调度主循环在锁内等待数千次独立写事务。
        keys = set()
        configs_valid = True
        with self.dispatch_lock:
            configuration = self.store.query("SELECT id,config FROM projects ORDER BY id")
            global_configuration = self.store.one("SELECT value FROM settings WHERE key='config'")
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
            keep = json.dumps(sorted(keys))
            doomed = []
            for row in self.store.query("SELECT key,value FROM settings WHERE key LIKE 'model-center:%' "
                                        "AND json_valid(value) AND json_extract(value,'$.fetched')<? "
                                        "AND key NOT IN (SELECT value FROM json_each(?)) LIMIT 4000",
                                        (stale, keep)):
                try:
                    fetched = json.loads(row["value"]).get("fetched")
                except (ValueError, TypeError, AttributeError):
                    continue
                if row["key"] not in keys and fetched and fetched < stale:
                    doomed.append(row["key"])
            if doomed:
                # 单事务批量删除，不再为每行借还连接；重复校验保留键与过期时间，
                # 避免删除扫描之后刚被刷新的目录。
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = [dict(row) for row in db.execute("SELECT id,config FROM projects ORDER BY id")]
                    global_row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
                    current_global = dict(global_row) if global_row else None
                    # 配置写入和 claim 共用数据库写事务；最终核验与删除不能隔开。
                    if (current == configuration and current_global == global_configuration and
                            not db.execute("SELECT 1 FROM runs WHERE ended IS NULL LIMIT 1").fetchone()):
                        db.executemany("DELETE FROM settings WHERE key=? AND json_valid(value) "
                                       "AND json_extract(value,'$.fetched')<? "
                                       "AND key NOT IN (SELECT value FROM json_each(?))",
                                       [(key, stale, keep) for key in doomed])
        cutoff_event = self.store.one("SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET 1000")
        if cutoff_event:
            self.store.execute("DELETE FROM events WHERE created<? AND seq<?", (now - 90 * 86400, cutoff_event["seq"]))
        # 保留删除留下的空闲页在 auto_vacuum=0 的库里不会归还给文件系统；
        # 门禁满足时压缩，备份/归档的容量预算才与真实数据量一致。
        _reclaim_database(self.store, self.dispatch_lock)
        # Retention has just shrunk the event log; refresh optimizer statistics after
        # the reclaim so the next query plans against real cardinalities instead of the
        # default table sizes.  This runs outside any explicit write transaction.
        refresh_query_planner(self.store)
        # Passive checkpoint yields to readers; archive checks are capped per cycle.
        self.store.wal_diagnostics(checkpoint=True)
        from .storage_tools import verify_archive
        prefix = "archive-verification:"
        archive_paths = [path for path in sorted((self.store.root / "archives").glob("*.sqlite3.gz"))
                         if not path.is_symlink()]
        for offset in range(0, len(archive_paths), ARCHIVE_VERIFICATION_READ_BATCH_SIZE):
            batch = archive_paths[offset:offset + ARCHIVE_VERIFICATION_READ_BATCH_SIZE]
            keys = [prefix + path.name for path in batch]
            placeholders = ",".join("?" for _ in keys)
            rows = self.store.query(
                "SELECT key,value FROM settings WHERE key IN (%s)" % placeholders,
                tuple(keys))
            verification_status = {row["key"]: row["value"] for row in rows}
            for path, key in zip(batch, keys):
                if path.is_symlink():
                    continue
                due, previous_type = _archive_verification_due(
                    verification_status.get(key, _ARCHIVE_VERIFICATION_MISSING), now)
                if not due:
                    continue
                if previous_type is not None:
                    # 只报形状，不回显原值：损坏内容可能是任意手工写入的文本。
                    try:
                        self.store.event("archive_verification_repaired",
                                         "Replaced unusable archive verification record: %s" % key,
                                         data={"key": key, "previous_type": previous_type})
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Archive verification repair event persistence failed: %s", key)
                try:
                    report = verify_archive(path, timeout=5)
                except (OSError, ValueError, TimeoutError, sqlite3.Error, EOFError, zlib.error) as exc:
                    report = {"valid": False, "checked_at": now, "error_type": type(exc).__name__}
                self.store.put_setting(key, report)
                break
            else:
                continue
            break
        self.store.put_setting("maintenance-last", now)
