#!/usr/bin/env python3
"""Fast documentation gate: local links, fragments and bilingual pairs."""
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"```.*?```", re.S)
HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


def tracked_markdown():
    output = subprocess.check_output(["git", "ls-files", "*.md"], cwd=ROOT, text=True)
    return [Path(line) for line in output.splitlines() if line]


def slug(value):
    value = value.strip().lower()
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"[^\w\u3400-\u9fff -]", "", value)
    return re.sub(r"\s+", "-", value).strip("-")


def anchors(path):
    return {slug(value) for value in HEADING.findall(FENCE.sub("", path.read_text(encoding="utf-8")))}


def check():
    errors = []
    files = tracked_markdown()
    for source in files:
        if source.parts and source.parts[0] == "skills":
            continue
        if source.name != "AGENTS.md":
            partner = source.with_name(source.stem + ".en.md") if not source.name.endswith(".en.md") else \
                source.with_name(source.name[:-6] + ".md")
            if not (ROOT / partner).is_file():
                errors.append(f"{source}: missing bilingual counterpart {partner}")
        text = FENCE.sub("", (ROOT / source).read_text(encoding="utf-8"))
        for target in LINK.findall(text):
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("mailto:"):
                continue
            relative = unquote(parsed.path)
            destination = (ROOT / source).parent / relative if relative else ROOT / source
            try:
                destination.resolve().relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{source}: local link escapes repository: {target}")
                continue
            if not destination.exists():
                errors.append(f"{source}: broken local link: {target}")
                continue
            if parsed.fragment and destination.suffix.lower() == ".md":
                if unquote(parsed.fragment).lower() not in anchors(destination):
                    errors.append(f"{source}: missing heading #{parsed.fragment} in {relative}")
    return errors


def main():
    errors = check()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print(f"Documentation links and bilingual pairs passed ({len(tracked_markdown())} files)")


if __name__ == "__main__":
    main()
