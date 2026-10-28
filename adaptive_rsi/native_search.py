"""Disposable regex worker: parent owns the timeout and cancellation."""
import json
import re
import sys


def main():
    print(json.dumps(search(json.load(sys.stdin))))


def search(payload):
    pattern, sensitive, lines, start, limit = payload
    compiled = re.compile(pattern, 0 if sensitive else re.IGNORECASE)
    matches = []
    following = None
    for number, line in enumerate(lines, 1):
        if number < start:
            continue
        if compiled.search(line):
            if len(matches) == limit:
                following = number
                break
            matches.append(number)
    return {"lines": matches, "next_line": following}


if __name__ == "__main__":
    main()
