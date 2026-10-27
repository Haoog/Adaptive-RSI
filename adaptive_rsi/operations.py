"""Bounded, inspectable background operations with durable terminal receipts."""
import concurrent.futures
import threading
import time

from .store import uid


class Operations:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rsi-operation")
        self.lock = threading.RLock()
        self.active = {}
        self.progress = {}
        self.store.execute("CREATE TABLE IF NOT EXISTS operations "
                           "(id TEXT PRIMARY KEY,kind TEXT,state TEXT,result TEXT,created REAL,ended REAL)")

    def recover(self):
        # Called only after the runtime owns the state directory lock.
        self.store.execute("UPDATE operations SET state='interrupted',ended=? WHERE ended IS NULL", (time.time(),))

    def busy(self):
        with self.lock:
            return bool(self.active)

    def start(self, kind, parameters):
        if kind not in ("backup", "diagnostics", "probe", "import", "restore-check"):
            raise ValueError("Unknown background operation")
        if not isinstance(parameters, dict):
            raise ValueError("Background operation parameters must be an object")
        with self.runtime.dispatch_lock, self.lock:
            self.runtime.updates.require_ready()
            if len(self.active) >= 4:
                raise ValueError("Background operations busy")
            operation = uid()
            stop = threading.Event()
            self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                               (operation, kind, "queued", time.time()))
            self.active[operation] = stop
            try:
                self.pool.submit(self._run, operation, kind, parameters, stop)
            except BaseException:
                self.active.pop(operation)
                self.store.execute("UPDATE operations SET state='interrupted',ended=? WHERE id=?", (time.time(), operation))
                raise
            return {"id": operation, "state": "queued"}

    def _run(self, operation, kind, parameters, stop):
        from .store import encode
        from . import storage_tools
        try:
            if stop.is_set():
                self.store.execute("UPDATE operations SET state='cancelled',ended=? WHERE id=?", (time.time(), operation))
                return
            self.store.execute("UPDATE operations SET state='running' WHERE id=?", (operation,))
            if kind == "backup":
                def progress(done, total):
                    # Writing to the source database from SQLite's backup callback
                    # can restart the copy indefinitely under sustained changes.
                    with self.lock:
                        self.progress[operation] = {"pages_done": done, "pages_total": total}
                result = storage_tools.backup(self.store, cancel=stop.is_set, progress=progress)
                storage_tools.retain_backups(self.store, max_count=20, max_age_days=90)
            elif kind == "diagnostics":
                result = storage_tools.diagnostics(self.store)
            elif kind == "probe":
                result = self.runtime.model_center.probe(parameters["provider"], parameters["model"],
                    parameters.get("effort", "default"), parameters.get("project"))
            elif kind == "restore-check":
                from .storage_tools import verify_backup
                identifier = parameters.get("id")
                if not isinstance(identifier, str) or len(identifier) != 16 or any(
                        char not in "0123456789abcdef" for char in identifier):
                    raise ValueError("Invalid backup ID")
                result = verify_backup(self.store.root / "backups" / (identifier + ".sqlite3"))
            else:
                result = {"id": self.runtime.import_project(parameters["url"], parameters.get("directory"))}
            self.store.execute("UPDATE operations SET state='completed',result=?,ended=? WHERE id=?",
                               (encode(result), time.time(), operation))
        except Exception as error:
            from .process import Cancelled
            from .server_validation import SAFE_ERROR_CODES
            code = getattr(error, "code", "operation_failed")
            self.store.execute("UPDATE operations SET state=?,result=?,ended=? WHERE id=?",
                ("cancelled" if isinstance(error, Cancelled) else "failed",
                 encode({"error_code": code if isinstance(code, str) and code in SAFE_ERROR_CODES else "operation_failed"}), time.time(), operation))
        finally:
            with self.lock:
                self.active.pop(operation, None)
                self.progress.pop(operation, None)
            self.runtime.wake.set()

    def get(self, operation):
        from .store import decode_object
        row = self.store.one("SELECT * FROM operations WHERE id=?", (operation,))
        if not row:
            raise ValueError("Operation not found")
        row["result"] = decode_object(row["result"])
        with self.lock:
            if row["ended"] is None and operation in self.progress:
                row["result"] = dict(self.progress[operation])
        row["cancellable"] = row["kind"] == "backup" and row["ended"] is None
        return row

    def cancel(self, operation):
        with self.lock:
            row = self.get(operation)
            if row["ended"] is not None:
                return row
            if not row["cancellable"]:
                raise ValueError("This operation cannot be cancelled safely")
            stop = self.active.get(operation)
            if stop:
                stop.set()
            return row

    def close(self):
        with self.lock:
            for stop in self.active.values():
                stop.set()
        self.pool.shutdown(wait=True)
