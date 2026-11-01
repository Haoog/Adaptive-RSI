"""Bounded, inspectable background operations with durable terminal receipts."""
import concurrent.futures
import json
import logging
import math
import re
import threading
import time
from pathlib import Path

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
        pending = []
        for path in (self.store.root / "operation-receipts").glob("*.json"):
            try:
                if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                    continue
                receipt = json.loads(path.read_text())
                if (not re.fullmatch(r"[a-f0-9]{16}", path.stem) or not isinstance(receipt, dict) or
                        receipt.get("state") not in ("completed", "failed", "cancelled") or
                        not isinstance(receipt.get("result"), dict) or
                        type(receipt.get("ended")) not in (int, float) or not math.isfinite(receipt["ended"])):
                    raise ValueError("Invalid terminal receipt")
                if not self._save_terminal(path.stem, receipt["state"], receipt["result"], receipt["ended"],
                                           existing_receipt=True):
                    pending.append(path.stem)
            except (OSError, ValueError, KeyError, TypeError):
                logging.getLogger(__name__).warning("Invalid operation receipt: %s", path.name)
        with self.store.connect() as db:
            rows = db.execute("SELECT id FROM operations WHERE ended IS NULL").fetchall()
            pending = set(pending)
            for row in rows:
                if row["id"] not in pending:
                    from .store import encode
                    db.execute("UPDATE operations SET state='interrupted',result=?,ended=? WHERE id=? AND ended IS NULL",
                               (encode({'outcome': 'unknown', 'reason': 'No durable terminal receipt; inspect effects before retrying'}),
                                time.time(), row["id"]))

    def _save_terminal(self, operation, state, result, ended=None, existing_receipt=False):
        from .store import encode
        from .updates import atomic_json
        ended = time.time() if ended is None else ended
        folder = self.store.root / "operation-receipts"
        path = folder / (operation + ".json")
        receipt_saved = existing_receipt
        if not existing_receipt:
            try:
                folder.mkdir(mode=0o700, exist_ok=True)
                atomic_json(path, {"state": state, "result": result, "ended": ended})
                receipt_saved = True
            except OSError as error:
                logging.getLogger(__name__).warning("Operation file receipt unavailable: %s (%s)", operation, type(error).__name__)
        try:
            self.store.execute("UPDATE operations SET state=?,result=?,ended=? WHERE id=? AND ended IS NULL",
                               (state, encode(result), ended, operation))
        except Exception as error:
            logging.getLogger(__name__).error("Operation receipt awaits database recovery: %s (%s)", operation, type(error).__name__)
            if not receipt_saved:
                raise RuntimeError("Operation result persistence failed in both database and receipt file") from None
            return False
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                logging.getLogger(__name__).warning("Committed operation file receipt cleanup failed: %s (%s)", operation, type(error).__name__)
            return True

    @staticmethod
    def _observe(future):
        error = future.exception()
        if error is not None:
            logging.getLogger(__name__).error("Background worker failed outside its result handler (%s)", type(error).__name__)

    def busy(self):
        with self.lock:
            return bool(self.active)

    def start(self, kind, parameters):
        if kind not in ("backup", "diagnostics", "probe", "import", "restore-check", "rehearse-restore", "rehearsal"):
            raise ValueError("Unknown background operation")
        if not isinstance(parameters, dict):
            raise ValueError("Background operation parameters must be an object")
        with self.runtime.dispatch_lock, self.lock:
            if self.runtime.stop.is_set():
                raise ValueError('Runtime is shutting down')
            self.runtime.updates.require_ready()
            if len(self.active) >= 4:
                raise ValueError("Background operations busy")
            operation = uid()
            stop = threading.Event()
            self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                               (operation, kind, "queued", time.time()))
            self.active[operation] = stop
            try:
                self.pool.submit(self._run, operation, kind, parameters, stop).add_done_callback(self._observe)
            except BaseException:
                self.active.pop(operation)
                self.store.execute("UPDATE operations SET state='interrupted',ended=? WHERE id=?", (time.time(), operation))
                raise
            return {"id": operation, "state": "queued"}

    def _execute(self, operation, kind, parameters, stop):
        from . import storage_tools
        from .process import Cancelled
        if stop.is_set():
            raise Cancelled("Background operation cancelled before execution")
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
        elif kind in ("rehearse-restore", "rehearsal"):
            from .storage_tools import rehearse_restore, rehearsal_report_path
            identifier = parameters.get("id")
            if identifier:
                if not isinstance(identifier, str) or len(identifier) != 16 or any(
                        char not in "0123456789abcdef" for char in identifier):
                    raise ValueError("Invalid backup ID")
                source = self.store.root / "backups" / (identifier + ".sqlite3")
            elif parameters.get("path") or parameters.get("file"):
                raw_path = parameters.get("path") or parameters.get("file")
                source = Path(str(raw_path))
            else:
                backups = sorted((p for p in (self.store.root / "backups").glob("*.sqlite3")
                                  if p.is_file() and not p.is_symlink()),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                if not backups:
                    raise ValueError("No backup available for rehearsal")
                source = backups[0]
            report_file = rehearsal_report_path(source, store=self.store, report_path=parameters.get("report"))
            result = rehearse_restore(source, report_path=report_file)
        else:
            result = {"id": self.runtime.import_project(parameters["url"], parameters.get("directory"))}
        return result

    def _run(self, operation, kind, parameters, stop):
        try:
            try:
                result = self._execute(operation, kind, parameters, stop)
                state = "completed"
            except Exception as error:
                from .process import Cancelled
                from .server_validation import SAFE_ERROR_CODES
                code = getattr(error, "code", "operation_failed")
                state = "cancelled" if isinstance(error, Cancelled) else "failed"
                result = {"error_code": code if isinstance(code, str) and code in SAFE_ERROR_CODES else "operation_failed"}
            # Retry persistence only; never rerun a completed business operation.
            for attempt in range(2):
                try:
                    self._save_terminal(operation, state, result)
                    break
                except RuntimeError:
                    if attempt:
                        raise
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
