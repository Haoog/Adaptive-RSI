"""Run an isolated desktop engine with temporary update endpoint unavailability."""

from pathlib import Path
import sys
from threading import Lock

from adaptive_rsi import lifecycle
from adaptive_rsi.desktop import run
from adaptive_rsi.server import Handler


def main(root, restarting):
    readiness = lifecycle.readiness
    respond = Handler.respond
    pending = {"status", "cancel"} if restarting else set()
    lock = Lock()
    unavailable = object()

    def guarded_readiness(runtime, action, version, lease):
        with lock:
            if (root / "fixture-unavailable").exists() or action in pending:
                pending.discard(action)
                with (root / "fixture-outages").open("a") as log:
                    log.write(f"{action}\n")
                return unavailable
        return readiness(runtime, action, version, lease)

    def report_unavailability(handler, data, *args, **kwargs):
        if data is unavailable:
            return respond(handler, {"error": "Temporarily unavailable"}, 503)
        return respond(handler, data, *args, **kwargs)

    lifecycle.readiness = guarded_readiness
    Handler.respond = report_unavailability
    run(root)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] == "restart")
