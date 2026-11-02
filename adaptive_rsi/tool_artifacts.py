"""Run-owned external storage for large native tool output.

Artifacts live outside the workspace, under the run's controller state
directory.  A tool result keeps its structural fields (exit code, error kind,
timeouts, pagination metadata, JSON shape); only a large text body is replaced
by a compact summary and an opaque artifact id.  The full body is readable only
by the invocation that produced it.

This module never calls a model.  It is deliberately independent of the native
adapter so the ownership and corruption rules can be tested in isolation.
"""
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

ARTIFACT_ID_PATTERN = re.compile(r"^art_[0-9a-f]{32}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# One body above this size is externalized.  Small results stay byte-for-byte
# compatible with the previous tool contract.
INLINE_BODY_BYTES = 16 * 1024
# The native loop used 128000 as its hard result envelope; keep that boundary.
MAX_RESULT_BYTES = 128000
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARTIFACTS = 128
DEFAULT_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_PAGE_BYTES = 16 * 1024
MAX_PAGE_BYTES = 32 * 1024
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_LINES = 200000
# Per-search result budget: stop at a line boundary and report a cursor instead
# of growing one response until the whole envelope has to be externalized.
SEARCH_RESULT_BYTES = 96000
_MAX_METADATA_BYTES = 64 * 1024


class ArtifactError(ValueError):
    """An artifact request is invalid or outside this invocation's ownership."""


class ArtifactBudgetError(ArtifactError):
    """The invocation's artifact byte/count budget would be exceeded."""


class ArtifactUnavailable(ArtifactError):
    """The artifact is missing, corrupt, expired or owned by another invocation."""


def _token(value, label):
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise ArtifactError("Invalid " + label)
    return value


def _line_count(text):
    return len(text.splitlines()) if text else 0


def _preview(text, limit=400):
    if len(text) <= limit:
        return text
    head = limit - 160
    return text[:head] + "\n...\n" + text[-(limit - head - 5):]


def _encoded_length(value):
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_RESULT_BYTES + 1


def artifact_reference(value) -> Optional[str]:
    """Return the first opaque artifact id in a nested tool result, if any."""
    if isinstance(value, dict):
        candidate = value.get("artifact")
        if isinstance(candidate, str) and ARTIFACT_ID_PATTERN.fullmatch(candidate):
            return candidate
        for item in value.values():
            found = artifact_reference(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = artifact_reference(item)
            if found:
                return found
    return None


def contains_artifact_reference(value) -> bool:
    return artifact_reference(value) is not None


class ArtifactStore:
    """A private directory scoped to one run and one model invocation."""

    def __init__(self, root, *, run, invocation, role, workspace,
                 max_artifact_bytes=MAX_ARTIFACT_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES,
                 max_artifacts=MAX_ARTIFACTS,
                 ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.time):
        self.run = _token(run, "run id")
        self.invocation = _token(invocation, "invocation id")
        self.role = _token(role, "role")
        self.workspace = Path(workspace).resolve()
        self.max_artifact_bytes = int(max_artifact_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.max_artifacts = int(max_artifacts)
        self.ttl_seconds = int(ttl_seconds)
        if min(self.max_artifact_bytes, self.max_total_bytes, self.max_artifacts,
               self.ttl_seconds) <= 0:
            raise ArtifactError("Artifact budgets must be positive")
        self.clock = clock
        root = Path(root).expanduser().resolve()
        if root == self.workspace or root in self.workspace.parents:
            raise ArtifactError("Artifact storage must be outside the workspace and its ancestors")
        try:
            root.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            # A workspace-visible artifact would let the implementation read
            # or alter sealed acceptance evidence.
            raise ArtifactError("Artifact storage must be outside the workspace")
        self.root = root
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.root.chmod(0o700)
        except OSError:
            raise ArtifactUnavailable("Artifact storage is unavailable") from None
        self._lock = threading.RLock()
        self._used_bytes = 0
        self._count = 0
        for metadata in self.root.glob("art_*.json"):
            if metadata.is_symlink():
                continue
            try:
                data = json.loads(metadata.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._used_bytes += max(0, int(data.get("bytes", 0)))
                    self._count += 1
            except (OSError, ValueError, TypeError):
                continue

    def _metadata_path(self, artifact):
        return self.root / (artifact + ".json")

    def _binary_path(self, artifact):
        return self.root / (artifact + ".bin")

    @staticmethod
    def _write_private(path, data):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o600)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def put(self, kind, text):
        """Store a complete text body and return its compact reference."""
        _token(kind, "artifact kind")
        if not isinstance(text, str):
            raise ArtifactError("Artifact content must be text")
        data = text.encode("utf-8")
        if len(data) > self.max_artifact_bytes:
            raise ArtifactBudgetError(
                "Artifact body is %d bytes; the per-artifact budget is %d bytes. Narrow the tool request."
                % (len(data), self.max_artifact_bytes))
        with self._lock:
            if self._count >= self.max_artifacts:
                raise ArtifactBudgetError(
                    "Artifact count budget (%d) is exhausted for this invocation." % self.max_artifacts)
            if self._used_bytes + len(data) > self.max_total_bytes:
                raise ArtifactBudgetError(
                    "Artifact total byte budget (%d) is exhausted for this invocation." % self.max_total_bytes)
            artifact = "art_" + uuid.uuid4().hex
            digest = hashlib.sha256(data).hexdigest()
            created = float(self.clock())
            metadata = {
                "id": artifact, "run": self.run, "invocation": self.invocation,
                "role": self.role, "kind": kind, "bytes": len(data),
                "lines": _line_count(text), "sha256": digest,
                "created": created, "expires": created + self.ttl_seconds,
            }
            binary = self._binary_path(artifact)
            sidecar = self._metadata_path(artifact)
            try:
                self._write_private(binary, data)
                self._write_private(sidecar, json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
            except OSError:
                for path in (binary, sidecar):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                raise ArtifactUnavailable("Artifact storage is unavailable") from None
            self._used_bytes += len(data)
            self._count += 1
        return {
            "artifact": artifact, "kind": kind, "bytes": len(data),
            "lines": metadata["lines"], "sha256": digest,
            "preview": _preview(text), "truncated": True,
            "reason": "result byte limit",
            "next_offset": 0,
            "summary": "%d bytes / %d lines stored outside the workspace; use read_artifact or search_artifact with this id"
                       % (len(data), metadata["lines"]),
        }

    def _load_metadata(self, artifact):
        if not isinstance(artifact, str) or not ARTIFACT_ID_PATTERN.fullmatch(artifact):
            raise ArtifactError("Invalid artifact id")
        path = self._metadata_path(artifact)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise ArtifactUnavailable("Artifact is unavailable for this invocation") from None
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                # A hardlink would alias a file outside the artifact directory.
                # The store creates each artifact exactly once, so any extra
                # link means the entry is no longer trustworthy.
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ArtifactUnavailable("Artifact is unavailable for this invocation")
                raw = stream.read(_MAX_METADATA_BYTES + 1)
        except OSError:
            raise ArtifactUnavailable("Artifact is unavailable for this invocation") from None
        if len(raw) > _MAX_METADATA_BYTES:
            raise ArtifactUnavailable("Artifact metadata is corrupt")
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ArtifactUnavailable("Artifact metadata is corrupt") from None
        if not isinstance(metadata, dict) or metadata.get("id") != artifact:
            raise ArtifactUnavailable("Artifact metadata is corrupt")
        if metadata.get("run") != self.run:
            raise ArtifactUnavailable("Artifact belongs to a different run")
        if metadata.get("invocation") != self.invocation:
            raise ArtifactUnavailable("Artifact is unavailable for this invocation")
        if metadata.get("role") != self.role:
            raise ArtifactUnavailable("Artifact belongs to a different role")
        if (type(metadata.get("bytes")) is not int or metadata["bytes"] < 0 or
                metadata["bytes"] > self.max_artifact_bytes or
                type(metadata.get("lines")) is not int or metadata["lines"] < 0 or
                not isinstance(metadata.get("kind"), str) or
                not _SAFE_TOKEN.fullmatch(metadata["kind"]) or
                not isinstance(metadata.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) or
                type(metadata.get("created")) not in (int, float) or
                type(metadata.get("expires")) not in (int, float)):
            raise ArtifactUnavailable("Artifact metadata is corrupt")
        if float(metadata["expires"]) <= float(self.clock()):
            raise ArtifactUnavailable("Artifact has expired; request the tool again")
        return metadata

    def _verified_bytes(self, metadata):
        path = self._binary_path(metadata["id"])
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise ArtifactUnavailable("Artifact is unavailable for this invocation") from None
        try:
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ArtifactUnavailable("Artifact is unavailable for this invocation")
                data = stream.read(metadata["bytes"] + 1)
        except OSError:
            raise ArtifactUnavailable("Artifact is unavailable for this invocation") from None
        if len(data) != metadata["bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ArtifactUnavailable("Artifact content is corrupt")
        return data

    def read(self, artifact, offset=0, limit=DEFAULT_PAGE_BYTES):
        """Read one UTF-8-safe page.  ``offset`` and ``limit`` are bytes.

        A page can exceed ``limit`` by at most three bytes so one multi-byte
        character is never split; the hard store maximum remains MAX_PAGE_BYTES.
        """
        if type(offset) is not int or offset < 0:
            raise ArtifactError("Artifact offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_BYTES:
            raise ArtifactError("Artifact page limit must be 1..%d bytes" % MAX_PAGE_BYTES)
        # JSON can expand each control byte to six bytes. Keep read pages below
        # the tool envelope even with escaped content, so reads stay inline.
        limit = min(limit, (MAX_RESULT_BYTES - 4096) // 6)
        metadata = self._load_metadata(artifact)
        data = self._verified_bytes(metadata)
        size = len(data)
        if offset > size:
            raise ArtifactError("Artifact offset exceeds its size")
        start = offset
        # Advance over continuation bytes if a caller supplied a non-boundary.
        while start < size and start - offset < 4 and (data[start] & 0xC0) == 0x80:
            start += 1
        window = data[start:start + limit + 3]
        try:
            text = window.decode("utf-8")
        except UnicodeDecodeError as exc:
            try:
                text = window[:exc.start].decode("utf-8")
            except UnicodeDecodeError:
                raise ArtifactUnavailable("Artifact content is not valid UTF-8") from None
        consumed = len(text.encode("utf-8"))
        next_offset = start + consumed
        eof = next_offset >= size
        return {"artifact": artifact, "offset": start,
                "next_offset": None if eof else next_offset, "eof": eof,
                "bytes": size, "lines": metadata.get("lines", 0),
                "content": text, "truncated": not eof,
                "reason": "page limit" if not eof else None,
                "kind": metadata.get("kind"),
                "sha256": metadata["sha256"]}

    def search(self, artifact, text, case_sensitive=True, regex=False, cursor=None,
               limit=DEFAULT_SEARCH_LIMIT):
        """Literal search across the complete artifact, with a line cursor."""
        if not isinstance(text, str) or not 1 <= len(text) <= 1000:
            raise ArtifactError("Artifact search requires 1..1000 characters")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ArtifactError("Artifact search limit must be 1..%d" % MAX_SEARCH_LIMIT)
        if regex:
            raise ArtifactError("Artifact search is literal-only; use the workspace search tool for regex")
        metadata = self._load_metadata(artifact)
        data = self._verified_bytes(metadata)
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ArtifactUnavailable("Artifact content is not valid UTF-8") from None
        lines = body.splitlines()
        signature = hashlib.sha256(json.dumps(
            [artifact, text, bool(case_sensitive)]).encode()).hexdigest()
        start_line = 1
        if cursor:
            try:
                stored_signature, start_line = json.loads(cursor)
                if (stored_signature != signature or type(start_line) is not int or
                        start_line < 1):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ArtifactError("Invalid artifact search cursor") from None
        needle = text if case_sensitive else text.casefold()
        matches = []
        reason = None
        next_line = None
        scanned = 0
        output_bytes = 0
        for number in range(start_line, len(lines) + 1):
            scanned += 1
            if scanned > MAX_SEARCH_LINES:
                reason, next_line = "scan limit", number
                break
            line = lines[number - 1]
            haystack = line if case_sensitive else line.casefold()
            if needle in haystack:
                if len(matches) >= limit:
                    reason, next_line = "match limit", number
                    break
                row = {"line": number, "text": line[:1000]}
                row_bytes = len(json.dumps(row).encode("utf-8"))
                if output_bytes + row_bytes > SEARCH_RESULT_BYTES:
                    reason, next_line = "result byte limit", number
                    break
                matches.append(row)
                output_bytes += row_bytes
        return {"artifact": artifact, "matches": matches, "truncated": reason is not None,
                "reason": reason, "limit": limit, "lines": len(lines),
                "bytes": metadata["bytes"], "next_cursor":
                    json.dumps([signature, next_line]) if next_line else None}


def compact_result(store, tool_name, value, inline_bytes=INLINE_BODY_BYTES) -> Any:
    """Replace only large text bodies, preserving the result's structure.

    ``run_command`` keeps every structural key.  Other JSON results keep their
    keys; a pathological result that is still over the envelope is stored whole
    as a JSON artifact rather than silently dropped.
    """
    if store is None:
        return value
    if isinstance(value, dict) and "exit_code" in value:
        result = dict(value)
        existing = result.get("artifacts")
        artifacts = dict(existing) if isinstance(existing, dict) else {}
        changed = False
        for key in ("stdout", "stderr"):
            body = result.get(key)
            if isinstance(body, str) and len(body.encode("utf-8")) > inline_bytes:
                reference = store.put(key, body)
                result[key] = reference
                artifacts[key] = reference["artifact"]
                changed = True
        if changed:
            result["artifacts"] = artifacts
            result["truncated"] = True
            result["reason"] = "result byte limit"
            return result
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > inline_bytes:
            return store.put(tool_name, value)
        return value
    if isinstance(value, dict):
        result = {key: compact_result(store, tool_name, item, inline_bytes)
                  for key, item in value.items()}
        if _encoded_length(result) > MAX_RESULT_BYTES:
            reference = store.put(tool_name + "_result", json.dumps(result, ensure_ascii=False))
            return {**reference, "structure": "json",
                    "truncated": True, "reason": "result byte limit",
                    "note": "The full structured result is stored as JSON text in this artifact."}
        return result
    if isinstance(value, list):
        result = [compact_result(store, tool_name, item, inline_bytes) for item in value]
        if _encoded_length(result) > MAX_RESULT_BYTES:
            reference = store.put(tool_name + "_result", json.dumps(result, ensure_ascii=False))
            return {**reference, "structure": "json",
                    "truncated": True, "reason": "result byte limit",
                    "note": "The full structured result is stored as JSON text in this artifact."}
        return result
    return value
