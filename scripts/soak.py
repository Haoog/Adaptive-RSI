"""Run an isolated, model-free Store lifecycle and resource-growth soak."""
import argparse
import concurrent.futures
import json
import resource
import threading
import tempfile
import time
import tracemalloc
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_rsi.store import Store
from adaptive_rsi.config import validate


def runtime_cycles(store, count):
    from adaptive_rsi.runtime import Runtime
    from adaptive_rsi.server import Server
    import urllib.request
    baseline_threads = threading.active_count()
    descriptors = Path("/proc/self/fd") if Path("/proc/self/fd").exists() else Path("/dev/fd")
    baseline_fds = len(list(descriptors.iterdir()))
    for index in range(count):
        runtime = Runtime(store, agent=object())
        server = None
        thread = None
        try:
            runtime.start(paused=True)
            server = Server(runtime)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            task = store.add_task("p", "goal", "runtime-soak-" + str(index), "Never invoke a model")
            runtime.cancel_task(task)
            for endpoint in ("health", "state?project=p&sections=core"):
                request = urllib.request.Request(server.local_origin + "/api/" + endpoint,
                    headers={"Cookie": server.cookie + "=" + server.token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    json.load(response)
            if store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"] != "cancelled":
                raise RuntimeError("Runtime soak lost cancellation")
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(5)
                if thread.is_alive():
                    raise RuntimeError("Runtime soak leaked its HTTP server")
            runtime.close()
        if store.metrics()["borrowed"] or threading.active_count() > baseline_threads + 2:
            raise RuntimeError("Runtime soak leaked a connection or worker")
    final_fds = len(list(descriptors.iterdir()))
    if final_fds > baseline_fds + 8:
        raise RuntimeError("Runtime soak descriptor growth exceeded its budget")
    return {"cycles": count, "fd_before": baseline_fds, "fd_after": final_fds,
            "threads_before": baseline_threads, "threads_after": threading.active_count(), "model_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--runtime-cycles", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000000 or not 0 <= args.seconds <= 86400 or not 0 <= args.runtime_cycles <= 10000:
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
            interactions = runtime_cycles(store, args.runtime_cycles) if args.runtime_cycles else None
            current, peak = tracemalloc.get_traced_memory()
            metrics = store.metrics()
            print(json.dumps({"iterations": index + 1, "seconds": time.monotonic() - began,
                              "python_bytes": current, "peak_python_bytes": peak,
                              "max_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              "pool_connections": store._pool.qsize(), "metrics": metrics,
                              "runtime_interactions": interactions,
                              "relationship_audit": store.relationship_audit(), "integrity": "ok"}))
        finally:
            store.close()
            tracemalloc.stop()


if __name__ == "__main__":
    main()
