"""Disposable HTTP worker; cancellation terminates the socket-owning process."""
import json
import sys

from .native import ProviderUnavailable, post_json
from .provider_health import ModelFailure


def main():
    try:
        response = post_json(*json.load(sys.stdin))
        result = {"ok": True, "response": response}
    except (ModelFailure, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc), "code": getattr(exc, "code", "execution"),
                  "unavailable": isinstance(exc, ProviderUnavailable),
                  "retry_after": getattr(exc, "retry_after", 0)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
