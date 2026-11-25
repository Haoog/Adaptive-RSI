"""Run an isolated desktop engine with temporary update endpoint unavailability."""

from pathlib import Path
import sys
from threading import Lock

from adaptive_rsi import lifecycle
from adaptive_rsi.desktop import run
from adaptive_rsi.store import StoreBusy


def main(root, restarting):
    readiness = lifecycle.readiness
    pending = {"status", "cancel"} if restarting else set()
    lock = Lock()

    def guarded_readiness(runtime, action, version, lease):
        with lock:
            if (root / "fixture-unavailable").exists() or action in pending:
                pending.discard(action)
                with (root / "fixture-outages").open("a") as log:
                    log.write(f"{action}\n")
                raise StoreBusy("fixture endpoint temporarily unavailable")
        return readiness(runtime, action, version, lease)

    lifecycle.readiness = guarded_readiness
    run(root)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] == "restart")
