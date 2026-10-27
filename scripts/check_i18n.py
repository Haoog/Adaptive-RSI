"""Check literal UI translations, dynamic message dictionaries and duplicate keys."""
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRING = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''


def check():
    source = (ROOT / "adaptive_rsi/web/i18n.js").read_text(encoding="utf-8")
    duplicates = []
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result
    translations = json.loads(source[source.index("{"):source.index("\n};") + 2], object_pairs_hook=pairs)
    errors = ["Duplicate translation: " + value for value in duplicates]
    for key, value in translations.items():
        if not isinstance(value, str) or not value.strip():
            errors.append("Empty English translation: " + key)
    for path in (ROOT / "adaptive_rsi/web").glob("*.js"):
        if path.name == "i18n.js":
            continue
        text = path.read_text(encoding="utf-8")
        literals = [(match[1], match.start()) for match in re.finditer(r"\bt\(\s*(" + STRING + ")", text)]
        if path.name in ("app.js", "model-center.js"):
            # These dictionaries are passed through t() after lookup.
            names = ("apiErrors", "statusNames", "sourceNames", "roleNames", "errors")
            for name in names:
                block = re.search(r"const " + name + r"\s*=\s*\{(.*?)\};", text, re.S)
                if block:
                    literals.extend((match[1], block.start()) for match in re.finditer(r":\s*(" + STRING + ")", block[1]))
        for literal, position in literals:
            try:
                key = ast.literal_eval(literal)
            except (SyntaxError, ValueError):
                continue
            if re.search(r"[\u3400-\u9fff]", key) and key not in translations:
                errors.append(str(path.relative_to(ROOT)) + ":" + str(text.count("\n", 0, position) + 1) + ": " + key)
    return errors


if __name__ == "__main__":
    errors = check()
    if errors:
        raise SystemExit("\n".join(errors))
    print("UI translation completeness passed")
