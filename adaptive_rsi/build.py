"""Content identity independent of the public semantic version."""
import hashlib
from pathlib import Path


def identity(root=None):
    root = Path(root or Path(__file__).parent)
    digest = hashlib.sha256()
    for path in sorted(list(root.glob("*.py")) + list((root / "web").glob("*"))):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


BUILD_ID = identity()
