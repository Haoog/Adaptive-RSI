"""Run an isolated, model-free Store lifecycle and resource-growth soak."""
import argparse
import concurrent.futures
import json
import resource
import tempfile
import time
import tracemalloc
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_rsi.store import Store
from adaptive_rsi.config import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000000 or not 0 <= args.seconds <= 86400:
        parser.error("Use 1..1000000 iterations and 0..86400 seconds")
    began = time.monotonic()
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="rsi-soak-") as directory:
        store = Store(Path(directory))
        config = validate({})
        try:
            store.execute("INSERT INTO projects VALUES ('p','soak','/unused','{}',0,0)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                for index in range(args.iterations):
                    task = store.add_task("p", "goal", str(index), "Isolated soak")
                    run = store.claim("p", config)
                    if run is None or run["task"] != task:
                        raise RuntimeError("Soak lost queue ownership")
                    store.transition(run["id"], "completed", {}, True)
                    list(pool.map(lambda _: store.query("SELECT count(*) AS n FROM runs"), range(8)))
                    if index % 17 == 0:
                        # Exercise restart and replacement detection without trusting a live pool.
                        store.close()
                    if index % 29 == 0:
                        # Simulate a process dying while a call remains reserved; recovery must
                        # make it observable and leave the next claim runnable.
                        reserved = store.reserve_call("p", run["id"], "review", config["models"]["review"], config)
                        store.execute("UPDATE calls SET state='interrupted' WHERE id=?", (reserved,))
                    if index % 100 == 0:
                        store.close()
                    if args.seconds and time.monotonic() - began >= args.seconds:
                        break
            with store.connect() as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Soak database integrity failed")
            current, peak = tracemalloc.get_traced_memory()
            metrics = store.metrics()
            print(json.dumps({"iterations": index + 1, "seconds": time.monotonic() - began,
                              "python_bytes": current, "peak_python_bytes": peak,
                              "max_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              "pool_connections": store._pool.qsize(), "metrics": metrics,
                              "relationship_audit": store.relationship_audit(), "integrity": "ok"}))
        finally:
            store.close()
            tracemalloc.stop()


if __name__ == "__main__":
    main()
