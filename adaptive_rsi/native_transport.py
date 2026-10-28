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
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("operation") == "regex_search":
            from .native_search import search
            print(json.dumps(search(payload["arguments"])))
            return
        response = post_json(*payload)
        result = {"ok": True, "response": response,
                  "request_id": getattr(response, "provider_request_id", None)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "code": getattr(exc, "code", "execution"),
                  "unavailable": isinstance(exc, ProviderUnavailable),
                  "retry_after": getattr(exc, "retry_after", 0),
                  "request_id": getattr(exc, "provider_request_id", None),
                  "request_outcome": getattr(exc, "request_outcome", "unknown"),
                  "possibly_billed": getattr(exc, "possibly_billed", True),
                  "retry_may_duplicate": getattr(exc, "retry_may_duplicate", True),
                  "stop_reason": getattr(exc, "stop_reason", None)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
