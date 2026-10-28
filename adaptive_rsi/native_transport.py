"""Disposable HTTP worker; cancellation terminates the socket-owning process."""
import json
import sys

from .native import ProviderUnavailable, post_json
from .provider_health import ModelFailure


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValueError("Empty transport payload")
        response = post_json(*json.loads(raw))
        result = {"ok": True, "response": response,
                  "request_id": getattr(response, "provider_request_id", None)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "code": getattr(exc, "code", "execution"),
                  "unavailable": isinstance(exc, ProviderUnavailable),
                  "retry_after": getattr(exc, "retry_after", 0),
                  "request_id": getattr(exc, "provider_request_id", None),
                  "stop_reason": getattr(exc, "stop_reason", None)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
