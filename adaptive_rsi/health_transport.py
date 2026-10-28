"""Isolated health probe; the parent owns its absolute deadline and cancellation."""
import json
import sys


def main(arguments=None):
    from .release import direct_health_check
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 3:
        raise ValueError("Health transport requires target, commit and deadline")
    url, sha, seconds = arguments
    timeout = float(seconds)
    if not 0 < timeout <= 60:
        raise ValueError("Invalid health transport deadline")
    print(json.dumps(direct_health_check(url, sha, timeout)))


if __name__ == "__main__":
    main()
