"""Background artifact retention and bounded state maintenance."""
import json
import threading
import time
from pathlib import Path


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
        now = time.time()
        if now - self.store.setting("maintenance-last", 0) < 3600:
            return
        with self.scheduler_scope("credential_cleanup"):
            self.model_center.cleanup_credentials()
        for project in self.store.query("SELECT * FROM projects"):
            with self.scheduler_scope("maintenance_project", project["id"]):
                config = self.config(project["id"])
                cutoff = now - config["limits"]["retention_days"] * 86400
                for run in self.store.query("SELECT id,path,state,task,result FROM runs WHERE project=? AND ended<? AND path IS NOT NULL", (project["id"], cutoff)):
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
        self.store.execute("DELETE FROM skills WHERE expires<?", (now - 30 * 86400,))
        self.store.execute("DELETE FROM phase_times WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?)", (stale, stale))
        self.store.execute("DELETE FROM remote_waits WHERE ended IS NOT NULL AND ended<? AND run IN "
                           "(SELECT id FROM runs WHERE ended IS NOT NULL AND ended<?)", (stale, stale))
        self.store.execute("DELETE FROM task_revisions WHERE created<? AND task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state NOT IN ('queued','planning','executing','evaluating'))", (stale, stale))
        self.store.execute("DELETE FROM submissions WHERE task IN "
                           "(SELECT id FROM tasks WHERE updated<? AND state IN ('merged','obsolete','cancelled','expired','reverted','superseded','pr_closed'))", (stale,))
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
                "AND NOT (r.state='superseded' AND coalesce(json_extract(r.result,'$.close_pending'),0)=1) AND (" +
                remaining + ") LIMIT 200", (stale,)):
            for prefix in prefixes:
                key = prefix + run["id"]
                self.store.execute("DELETE FROM settings WHERE key=? OR (key>=? AND key<?)",
                                   (key, key + ":", key + ";"))
        # 批量初筛决定已直接写入 task_decisions 并随任务持久；这里只清理
        # 升级前遗留的 triage-state 键（存量暂缓任务会在 reassess 中迁移）。
        for task in self.store.query("SELECT id FROM tasks WHERE state<>'deferred' AND updated<? LIMIT 200", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", ("triage-state:" + task["id"],))
        # 过滤在 LIMIT 之前完成：新鲜条目占满一批时，过期条目仍能在本轮被删除。
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'task-proof:%' AND "
                "(json_extract(value,'$.at') IS NULL OR json_extract(value,'$.at')<?) LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'provider-health:%' AND "
                "(json_extract(value,'$.at') IS NULL OR (json_extract(value,'$.paused')=0 AND json_extract(value,'$.at')<?)) LIMIT 400", (stale,)):
            self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        # Issue 评论缓存按 issue 数量累积（每个键含最多 20 条评论正文）；
        # 超过保留期未刷新的键不会再被命中（处理时总会刷新），直接删除。
        for row in self.store.query(
                "SELECT key FROM settings WHERE key LIKE 'issue-comments:%' AND "
                "(json_extract(value,'$.checked_at') IS NULL OR json_extract(value,'$.checked_at')<?) LIMIT 400", (stale,)):
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
                for row in self.store.query("SELECT key,value FROM settings WHERE key LIKE 'model-center:%' LIMIT 4000"):
                    try:
                        fetched = json.loads(row["value"]).get("fetched")
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if row["key"] not in keys and fetched and fetched < stale:
                        self.store.execute("DELETE FROM settings WHERE key=?", (row["key"],))
        self.store.execute("DELETE FROM events WHERE created<? AND seq NOT IN (SELECT seq FROM events ORDER BY seq DESC LIMIT 1000)", (now - 90 * 86400,))
        self.store.put_setting("maintenance-last", now)
