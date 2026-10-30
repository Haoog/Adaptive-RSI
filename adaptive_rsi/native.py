"""Native API adapters with a host-controlled, workspace-only tool loop."""
import json
import hashlib
import contextlib
import fnmatch
import concurrent.futures
import http.client
import logging
import os
import re
import stat
import sqlite3
import threading
import uuid
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .agent import SCHEMAS, TEXT, model_capabilities, object_schema, request_output_budget, validate_output
from .config import relative_path
from .credentials import Credentials
from .provider_health import ModelFailure, error_code, http_failure, provider_text_code
from .process import Cancelled, Sandbox, scrub_tree
from .tool_artifacts import (
    ArtifactError,
    DEFAULT_PAGE_BYTES,
    DEFAULT_SEARCH_LIMIT,
    MAX_RESULT_BYTES,
    ArtifactStore,
    compact_result,
    contains_artifact_reference,
)


class ProviderUnavailable(ModelFailure):
    request_outcome = "unknown"
    possibly_billed = True
    retry_may_duplicate = True
    def __init__(self, message, code=None):
        # Preserve compatibility for callers supplying legacy provider diagnostics.
        super().__init__(message, code or provider_text_code(message))
        self.failure_code = "provider"


class ProviderInterrupted(ModelFailure):
    fallback_allowed = False

    def __init__(self, cause):
        super().__init__("Provider became unavailable after workspace tools; fallback refused to avoid mixing implementations",
                         error_code(cause))
        self.retry_after = getattr(cause, "retry_after", 0)
        self.provider_id = getattr(cause, "provider_id", None)
        self.provider_request_id = getattr(cause, "provider_request_id", None)
        for field in ("request_outcome", "possibly_billed", "retry_may_duplicate"):
            setattr(self, field, getattr(cause, field, "unknown" if field == "request_outcome" else True))


def response_stop_reason(protocol, response):
    """Normalize only known stop signals; never persist provider-generated prose."""
    raw = response.get("stop_reason") or response.get("finishReason") or response.get("done_reason")
    choices = response.get("choices") or response.get("candidates")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    raw = raw or first.get("finish_reason") or first.get("finishReason")
    if protocol == "responses":
        detail = response.get("incomplete_details")
        raw = (detail.get("reason") if isinstance(detail, dict) else None) or raw or response.get("status")
        output = response.get("output")
        for item in output if isinstance(output, list) else []:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "refusal" for part in content):
                return "refusal"
    feedback = response.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        return "safety"
    message = first.get("message")
    if isinstance(message, dict) and message.get("refusal"):
        return "refusal"
    if not isinstance(raw, str):
        return "text_only"
    return {"max_tokens": "output_limit", "max_output_tokens": "output_limit", "length": "output_limit",
            "MAX_TOKENS": "output_limit", "content_filter": "safety", "SAFETY": "safety",
            "RECITATION": "safety", "BLOCKLIST": "safety", "PROHIBITED_CONTENT": "safety",
            "refusal": "refusal", "pause_turn": "paused", "stop_sequence": "stop_sequence",
            "end_turn": "text_only", "stop": "text_only", "STOP": "text_only",
            "completed": "text_only"}.get(raw, "unknown")


class ArgumentsError(ValueError):
    """Arguments of one tool call could not be parsed; reported back to the model instead of failing the run."""


# 输出上限截断后的 finish 修复次数上限，按一次 call 累计（不因中间的工具轮清零）；
# 超过后明确失败并提示缩短/拆分结果，而不是在同一预算上反复请求、反复计费。
MAX_OUTPUT_LIMIT_REPAIRS = 1


class ModelResponse(dict):
    provider_request_id = None


def provider_request_id(headers):
    for key in ("x-request-id", "request-id", "x-goog-request-id", "x-amzn-requestid"):
        value = headers.get(key) if headers else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            return value
    return None


def parse_arguments(name, raw):
    # One malformed tool call must not end the run; the model receives the reason and can correct itself.
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        return ArgumentsError("Arguments for tool '" + name + "' are not valid JSON: " + str(exc))
    if not isinstance(value, dict):
        return ArgumentsError("Arguments for tool '" + name + "' are not a JSON object")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_json(url, payload, headers, timeout, max_bytes, cancel=None):
    if cancel is not None:
        from .process import run_process
        argv = ([sys.executable, "internal-native-transport"] if getattr(sys, "frozen", False) else
                [sys.executable, "-m", "adaptive_rsi.native_transport"])
        result = run_process(argv,
                             cwd=Path(__file__).resolve().parents[1], timeout=timeout,
                             cancel=cancel, output_mb=max(1, max_bytes // (1024 * 1024) * 2),
                             input_data=json.dumps([url, payload, headers, timeout, max_bytes]).encode())
        if result["error_kind"] == "timeout":
            raise ProviderUnavailable("Model API connection timed out", "timeout")
        if result["error"] or result["exit_code"]:
            detail = (result.get("stderr") or result.get("stdout") or result.get("error") or "").strip()
            msg = "Model transport failed" + (f": {detail[:500]}" if detail else "")
            raise RuntimeError(msg)
        try:
            envelope = json.loads(result["stdout"].strip())
            if not isinstance(envelope, dict):
                raise ValueError("Envelope must be an object")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Model transport returned invalid JSON: " + str(exc)) from None
        if not envelope.get("ok"):
            problem = (ProviderUnavailable(envelope.get("error", "Model unavailable"), envelope.get("code"))
                       if envelope.get("unavailable") else
                       ModelFailure(envelope.get("error", "Model failure"), envelope.get("code")))
            problem.retry_after = envelope.get("retry_after", 0)
            for field, default in (("request_outcome", "unknown"), ("possibly_billed", True), ("retry_may_duplicate", True)):
                setattr(problem, field, envelope.get(field, default))
            problem.provider_request_id = provider_request_id({"x-request-id": envelope.get("request_id")})
            stop_reason = envelope.get("stop_reason")
            if isinstance(stop_reason, str) and stop_reason in {"output_limit", "safety", "refusal", "paused", "stop_sequence", "text_only", "unknown"}:
                problem.stop_reason = stop_reason
            raise problem
        value = envelope["response"]
        if not isinstance(value, dict):
            problem = ModelFailure("Model API response must be an object", "tool_format")
            problem.provider_request_id = provider_request_id({"x-request-id": envelope.get("request_id")})
            raise problem
        value = ModelResponse(value)
        value.provider_request_id = provider_request_id({"x-request-id": envelope.get("request_id")})
        return value
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json", **headers})
    request_id = None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            request_id = provider_request_id(response.headers)
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            problem = ModelFailure("Model response exceeds output limit", "tool_format")
            problem.stop_reason = "output_limit"
            problem.provider_request_id = request_id
            raise problem
        value = json.loads(raw)
        if not isinstance(value, dict):
            problem = ModelFailure("Model API response must be an object", "tool_format")
            problem.provider_request_id = request_id
            raise problem
        value = ModelResponse(value)
        value.provider_request_id = request_id
        return value
    except urllib.error.HTTPError as exc:
        # Provider error bodies may echo authentication headers or private prompts.
        error = "Model API HTTP " + str(exc.code)
        problem = http_failure(error + "; check endpoint, credentials and model capabilities", exc.code)
        if exc.code in [404, 429, 502, 503, 504]:
            problem = ProviderUnavailable(error, problem.code)
        problem.provider_request_id = provider_request_id(exc.headers)
        outcome = "rejected" if exc.code in (400, 401, 403, 404, 429) else "unknown"
        setattr(problem, "request_outcome", outcome)
        setattr(problem, "possibly_billed", outcome == "unknown")
        setattr(problem, "retry_may_duplicate", outcome == "unknown")
        from email.utils import parsedate_to_datetime
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        try:
            problem.retry_after = min(86400, max(0, float(retry) if retry.isdigit() else
                                                parsedate_to_datetime(retry).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            problem.retry_after = 0
        try:
            exc.close()
        except (AttributeError, KeyError):
            pass
        raise problem from None
    except (TimeoutError, ):
        problem = ProviderUnavailable("Model API connection timed out", "timeout")
        problem.provider_request_id = request_id
        raise problem from None
    except http.client.HTTPException:
        problem = ProviderUnavailable("Model API response connection was interrupted", "connection")
        problem.provider_request_id = request_id
        raise problem from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        code = "timeout" if isinstance(reason, TimeoutError) else "connection"
        problem = ProviderUnavailable("Model API connection failed or timed out", code)
        if isinstance(reason, ConnectionRefusedError):
            problem.request_outcome = "not_sent"
            problem.possibly_billed = False
            problem.retry_may_duplicate = False
        problem.provider_request_id = request_id
        raise problem from None
    except ValueError:
        problem = ModelFailure("Model API did not return valid JSON", "tool_format")
        problem.provider_request_id = request_id
        raise problem from None


def tools_for(role):
    if role == "probe":
        return [("finish", "Return summary RSI_OK to test structured tool output.", SCHEMAS[role])]
    listing = object_schema({})
    listing["properties"].update(offset={"type": "integer"}, limit={"type": "integer"})
    searching = object_schema({"text": TEXT})
    searching["properties"].update(path=TEXT, case_sensitive={"type": "boolean"}, regex={"type": "boolean"},
                                   cursor=TEXT, limit={"type": "integer"})
    artifact_reader = object_schema({"artifact": TEXT})
    artifact_reader["properties"].update(offset={"type": "integer"}, limit={"type": "integer"})
    artifact_search = object_schema({"artifact": TEXT, "text": TEXT})
    artifact_search["properties"].update(case_sensitive={"type": "boolean"}, cursor=TEXT,
                                         limit={"type": "integer"})
    tools = [
        ("list_files", "List source files with optional offset and limit (1..1000); inspect truncation metadata.", listing),
        ("read_file", "Read at most 500 lines. Paths must be relative to this snapshot.", object_schema({
            "path": TEXT, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}})),
        ("search", "Find text with optional path glob, case_sensitive, regex, limit (1..100), and continuation cursor. Regex has a per-file time limit.", searching),
        ("read_artifact", "Read a byte page of a large tool output stored by this invocation. Use the opaque artifact id from the tool result; offset and limit are bytes (limit at most 32768).", artifact_reader),
        ("search_artifact", "Search a large tool output stored by this invocation literally; returns line numbers and a continuation cursor. Only this invocation's artifacts are readable.", artifact_search),
    ]
    if role == "implementation":
        tools += [
            ("write_file", "Create or replace a UTF-8 source file in the approved scope.", object_schema({"path": TEXT, "content": TEXT})),
            ("replace_text", "Replace one unique exact text match in a source file.", object_schema({"path": TEXT, "old": TEXT, "new": TEXT})),
            ("run_command", "Run a command in the network-disabled sandbox. No credentials or other projects are available.",
             object_schema({"argv": {"type": "array", "items": TEXT}, "cwd": TEXT})),
        ]
    return tools + [("finish", "Submit the final structured result. Do not combine with other tool calls.", SCHEMAS[role])]


class WorkspaceTools:
    cancel: Optional[Callable[[], bool]]
    def __init__(self, workspace, role, state, config, cancel, protected,
                 run=None, invocation=None, artifact_root=None):
        self.workspace = Path(workspace).resolve()
        self.role, self.state, self.config = role, state, config
        self.cancel, self.protected = cancel, protected
        self.changed = False
        self._file_cache = None
        self._text_cache = {}
        self._text_cache_bytes = 0
        self.files_truncated = False
        self.index_source = "filesystem_fallback"
        self.scope = None
        self.deadline = None
        self._artifact_spec = None
        self._artifact_store = None
        self._artifact_lock = threading.Lock()
        if run is not None or invocation is not None:
            if run is None or invocation is None:
                raise ValueError("Artifact storage requires both run and invocation identities")
            root = (Path(artifact_root) if artifact_root is not None else
                    Path(state) / "runs" / str(run) / "artifacts")
            self._artifact_spec = (root, str(run), str(invocation))

    def artifact_store(self):
        """Create the invocation-owned store lazily; never inside the workspace."""
        if self._artifact_spec is None:
            return None
        if self._artifact_store is None:
            with self._artifact_lock:
                if self._artifact_store is None:
                    root, run, invocation = self._artifact_spec
                    self._artifact_store = ArtifactStore(
                        root, run=run, invocation=invocation, role=self.role,
                        workspace=self.workspace)
        return self._artifact_store

    def remaining(self):
        if self.deadline is None:
            return self.config["limits"]["command_seconds"]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnavailable("Native conversation timed out", "timeout")
        return remaining

    @contextlib.contextmanager
    def parent_descriptor(self, path, create=False):
        parts = path.relative_to(self.workspace).parts
        if not parts:
            raise ValueError("Source must be a file")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.workspace, flags)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                following = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = following
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read_source(self, path, maximum):
        with self.parent_descriptor(path) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Source must be a regular file")
            data = bytearray()
            while len(data) <= maximum:
                self.check_cancelled()
                chunk = stream.read(min(65536, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("Source file is too large")
            self.check_cancelled()
            return data.decode("utf-8")

    def write_source(self, path, text):
        with self.parent_descriptor(path, create=True) as (parent, leaf):
            temporary = ".rsi-write-" + uuid.uuid4().hex
            try:
                old = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(old.st_mode):
                    raise ValueError("Source must be a regular file")
                mode = stat.S_IMODE(old.st_mode)
            except FileNotFoundError:
                mode = 0o644
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(text.encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), mode)
                self.remaining()
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def check_cancelled(self):
        self.remaining()
        if self.cancel and self.cancel():
            raise Cancelled("Cancelled during source inspection")

    def path(self, value):
        relative_path(value)
        if any(part.startswith(".rsi-") for part in Path(value).parts):
            raise ValueError("Internal agent paths are not source files")
        path = (self.workspace / value).resolve()
        path.relative_to(self.workspace)
        if ".git" in path.relative_to(self.workspace).parts or any(p.startswith(".rsi-") for p in path.relative_to(self.workspace).parts):
            raise ValueError("Internal metadata is protected")
        return path

    def files(self):
        if self._file_cache is not None:
            return self._file_cache
        # Let Git interpret nested negations and tracked-file exceptions. Bound
        # subprocess output and time; never silently use a truncated Git listing.
        if (self.workspace / ".git").exists():
            from .process import run_process
            command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                       "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
            ignore = self.workspace / ".rsiignore"
            if ignore.is_file() and not ignore.is_symlink():
                command.append("--exclude-from=.rsiignore")
            outcome = run_process(command, cwd=self.workspace, timeout=min(10, self.remaining()),
                                  cancel=self.cancel, output_mb=4,
                                  env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
            if not outcome["error"] and not outcome["exit_code"]:
                self.index_source = "git"
                result = []
                for name in sorted(set(outcome["stdout"].split("\0"))):
                    self.check_cancelled()
                    if not name:
                        continue
                    try:
                        path = self.path(name)
                        if any((self.workspace.joinpath(*Path(name).parts[:i])).is_symlink()
                               for i in range(1, len(Path(name).parts) + 1)):
                            continue
                        if not path.is_file():
                            continue
                    except (ValueError, OSError):
                        continue
                    if len(result) >= 4000:
                        self.files_truncated = True
                        break
                    result.append(name)
                self._file_cache = result
                return result
            self.index_source = "filesystem_fallback_git_unavailable"
        result = []
        for root, dirs, files in os.walk(self.workspace, followlinks=False):
            self.check_cancelled()
            dirs[:] = sorted(d for d in dirs if d != ".git" and not d.startswith(".rsi-")
                             and d not in ["node_modules", "__pycache__", ".venv"] and not (Path(root) / d).is_symlink())
            for name in sorted(files):
                path = Path(root) / name
                if path.is_file() and not path.is_symlink() and not name.startswith(".rsi-"):
                    if len(result) >= 4000:
                        self.files_truncated = True
                        self._file_cache = sorted(result)
                        return self._file_cache
                    result.append(path.relative_to(self.workspace).as_posix())
        self._file_cache = sorted(result)
        return self._file_cache

    def search(self, args):
        if not args["text"] or len(args["text"]) > 1000:
            raise ValueError("Search requires 1..1000 characters")
        result = []
        limit = args.get("limit", 100)
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be 1..100")
        glob = args.get("path", "*")
        if len(glob) > 1000 or glob.startswith("/") or ".." in Path(glob).parts:
            raise ValueError("Invalid search path glob")
        regex = args.get("regex", False)
        if regex:
            try:
                re.compile(args["text"])
            except re.error:
                raise ValueError("Invalid regular expression") from None
        signature = hashlib.sha256(json.dumps([args["text"], glob, args.get("case_sensitive", True), regex]).encode()).hexdigest()
        start_file, start_line = "", 1
        if args.get("cursor"):
            try:
                sig, start_file, start_line = json.loads(args["cursor"])
                if sig != signature or not isinstance(start_file, str) or type(start_line) is not int or start_line < 1:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("Invalid search cursor") from None
        skipped = 0
        output_bytes = 0
        def add_match(file, number, text):
            nonlocal output_bytes
            row = {"path": file, "line": number, "text": text[:1000]}
            size = len(json.dumps(row).encode())
            if output_bytes + size > 96000:
                return False
            result.append(row)
            output_bytes += size
            return True
        def page(reason=None, file=None, line=1):
            return {"matches": result, "truncated": reason is not None or self.files_truncated or skipped > 0,
                    "reason": reason or ("file limit" if self.files_truncated else "unreadable or oversized files" if skipped else None),
                    "limit": limit, "skipped_files": skipped, "index_source": self.index_source,
                    "next_cursor": json.dumps([signature, file, line]) if file else None}
        needle = args["text"] if args.get("case_sensitive", True) else args["text"].casefold()
        remaining = 12 * 1024 * 1024
        try:
            for file in self.files():
                self.check_cancelled()
                if file < start_file or not fnmatch.fnmatchcase(file, glob):
                    continue
                path = self.path(file)
                if path.stat().st_size > 1024 * 1024:
                    skipped += 1
                    continue
                try:
                    if remaining < path.stat().st_size:
                        return page("search byte limit", file)
                    text = self._text_cache.get(file)
                    if text is None:
                        text = self.read_source(path, 1024 * 1024)
                        size = len(text.encode("utf-8"))
                        if self._text_cache_bytes + size <= 4 * 1024 * 1024:
                            self._text_cache[file] = text
                            self._text_cache_bytes += size
                    remaining -= len(text.encode("utf-8"))
                    lines = text.splitlines()
                except (UnicodeError, ValueError, OSError):
                    skipped += 1
                    continue
                if regex:
                    from .process import run_process
                    argv = ([sys.executable, "internal-native-transport"] if getattr(sys, "frozen", False) else
                            [sys.executable, "-m", "adaptive_rsi.native_transport"])
                    outcome = run_process(argv,
                                          cwd=Path(__file__).resolve().parents[1], timeout=min(2, self.remaining()), cancel=self.cancel,
                                          output_mb=1, input_data=json.dumps({"operation": "regex_search", "arguments": [args["text"], args.get("case_sensitive", True),
                                                                            lines, start_line if file == start_file else 1,
                                                                            limit - len(result)]}).encode())
                    if outcome["error"] or outcome["exit_code"]:
                        return page("regex time limit" if outcome.get("error_kind") == "timeout" else "regex worker failure", file)
                    found = json.loads(outcome["stdout"])
                    for i in found["lines"]:
                        if not add_match(file, i, lines[i - 1]):
                            return page("result byte limit", file, i)
                    if found["next_line"] is not None:
                        return page("match limit", file, found["next_line"])
                    continue
                for i, line in enumerate(lines, 1):
                    if file == start_file and i < start_line:
                        continue
                    if i % 256 == 0:
                        self.check_cancelled()
                    if needle in (line if args.get("case_sensitive", True) else line.casefold()):
                        if len(result) >= limit:
                            return page("match limit", file, i)
                        if not add_match(file, i, line):
                            return page("result byte limit", file, i)
            return page()
        except Cancelled as exc:
            exc.partial_results = [{"path": row["path"], "line": row["line"]} for row in result[:100]]
            raise

    def execute(self, name, args) -> Any:
        self.remaining()
        if self.cancel and self.cancel():
            raise Cancelled("Cancelled before model tool execution")
        schema = next((schema for tool, _, schema in tools_for(self.role) if tool == name and tool != "finish"), None)
        if schema is None:
            raise ValueError("Tool not available for this role")
        try:
            validate_output(args, schema)
        except RuntimeError as exc:
            raise ArgumentsError("Tool arguments failed schema validation: " + str(exc)) from None
        if name == "list_files":
            files = self.files()
            offset, limit = args.get("offset", 0), args.get("limit", 1000)
            if offset < 0 or not 1 <= limit <= 1000:
                raise ValueError("Invalid file pagination")
            following = offset + limit if offset + limit < len(files) else None
            return {"files": files[offset:offset + limit], "truncated": following is not None or self.files_truncated,
                    "limit": limit, "offset": offset, "next_offset": following,
                    "reason": "page limit" if following is not None else "file limit" if self.files_truncated else None,
                    "index_truncated": self.files_truncated, "index_source": self.index_source,
                    "ignore_rules_applied": self.index_source == "git"}
        if name == "read_file":
            start, end = args["start_line"], args["end_line"]
            if not 1 <= start <= end <= start + 499:
                raise ValueError("Read requires 1-based range of at most 500 lines")
            path = self.path(args["path"])
            rows, scanned = [], 0
            with self.parent_descriptor(path) as (parent, leaf):
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Source must be a regular file")
                for number in range(1, end + 1):
                    self.check_cancelled()
                    line = source.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    scanned += len(line)
                    if len(line) > 1024 * 1024 or scanned > 32 * 1024 * 1024:
                        raise ValueError("Source read exceeds scan budget; narrow the range")
                    if number >= start:
                        rows.append(str(number) + ": " + line.decode("utf-8").rstrip("\r\n"))
            return "\n".join(rows)
        if name == "search":
            return self.search(args)
        if name in ("read_artifact", "search_artifact"):
            store = self.artifact_store()
            if store is None:
                raise ValueError("Artifact storage is unavailable for this invocation")
            if name == "read_artifact":
                return store.read(args["artifact"], offset=args.get("offset", 0),
                                  limit=args.get("limit", DEFAULT_PAGE_BYTES))
            return store.search(args["artifact"], args["text"],
                                case_sensitive=args.get("case_sensitive", True),
                                cursor=args.get("cursor"),
                                limit=args.get("limit", DEFAULT_SEARCH_LIMIT))
        if name in ["write_file", "replace_text"]:
            path = self.path(args["path"])
            relative = path.relative_to(self.workspace).as_posix()
            if self.scope is not None and not any(scope == "." or relative == scope or
                    relative.startswith(scope.rstrip("/") + "/") for scope in self.scope):
                raise ValueError("Source write is outside the approved plan scope")
            if name == "replace_text":
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Source file is too large")
                text = self.read_source(path, 1024 * 1024)
                if not args["old"] or text.count(args["old"]) != 1:
                    raise ValueError("Replacement requires one unique match")
                text = text.replace(args["old"], args["new"], 1)
            else:
                text = args["content"]
            if len(text.encode()) > 1024 * 1024:
                raise ValueError("Source write exceeds 1 MiB")
            self.write_source(path, text)
            self.changed = True
            self._file_cache = None
            self._text_cache.clear()
            self._text_cache_bytes = 0
            return {"written": args["path"]}
        if name == "run_command":
            relative_path(args["cwd"])
            if not args["argv"] or any("\0" in x or len(x) > 4000 for x in args["argv"]):
                raise ValueError("Invalid command arguments")
            self.changed = True
            self._file_cache = None
            self._text_cache.clear()
            self._text_cache_bytes = 0
            config = {**self.config, "limits": {**self.config["limits"], "command_seconds": self.remaining()}}
            sandbox = Sandbox(self.state, config)
            writable = None
            if self.scope is not None:
                writable = [self.path(scope) for scope in self.scope]
                writable += [self.workspace / ".rsi-home", self.workspace / ".rsi-tmp"]
                for item in writable[-2:]:
                    item.mkdir(exist_ok=True)
            result = sandbox.check({"name": "model tool", **args}, self.workspace, self.cancel,
                                   protected=[self.workspace / ".git"], hidden=self.protected, writable=writable)
            scrub_tree(self.workspace)
            # Preserve every structural field (exit code, error kind, timing);
            # execute_tools replaces only a large stdout/stderr body with an
            # artifact reference.  timed_out is an explicit compatibility alias.
            value = {key: item for key, item in result.items() if key not in ("argv", "name")}
            value["timed_out"] = bool(result.get("error_kind") == "timeout" or
                                      result.get("error") == "Command timed out")
            return value
        raise ValueError("Unknown tool")


def _bounded_tool_value(value, error=None):
    """Legacy bounded fallback used only when external storage is unavailable."""
    detail = {"artifact_error": str(error)} if error else {}
    if isinstance(value, dict) and "exit_code" in value:
        def tail(body):
            return body[-24000:] if isinstance(body, str) else body
        return {**value, "stdout": tail(value.get("stdout", "")),
                "stderr": tail(value.get("stderr", "")), "truncated": True, **detail}
    return {"error": "Tool output exceeds limit; narrow the request", **detail}


def execute_tools(tools, calls):
    factory = getattr(tools, "artifact_store", None)
    artifact_store: Any
    try:
        artifact_store = factory() if callable(factory) else None
    except (ArtifactError, OSError, ValueError):
        # A state/workspace layout that cannot provide private storage keeps
        # the old bounded behavior rather than writing artifacts into source.
        artifact_store = None

    def execute(call, isolated=False):
        call_id, name, args = call
        tools.check_cancelled()
        if isinstance(args, ArgumentsError):
            return call_id, name, {"error": str(args)}
        worker = tools
        if isolated:
            worker = WorkspaceTools(tools.workspace, tools.role, tools.state, tools.config,
                                    tools.cancel, tools.protected)
            worker.deadline, worker.scope = tools.deadline, tools.scope
            worker._file_cache = tools._file_cache
            worker.files_truncated, worker.index_source = tools.files_truncated, tools.index_source
            worker._artifact_spec = tools._artifact_spec
            worker._artifact_store = artifact_store
        callback = getattr(tools, "trace", None)
        def trace(detail):
            if callback:
                try:
                    callback(detail)
                except (OSError, sqlite3.Error):
                    if not getattr(tools, "trace_warning", False):
                        tools.trace_warning = True
                        print("Native tool trace persistence unavailable", file=sys.stderr)
        identity = {"invocation": uuid.uuid4().hex,
                    "tool_call_hash": hashlib.sha256(json.dumps(call_id).encode()).hexdigest(),
                    "tool": name if name in {item[0] for item in tools_for(tools.role)} else "unknown",
                    "arguments_hash": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()}
        began = time.monotonic()
        trace({**identity, "state": "started"})
        try:
            value = worker.execute(name, args)
            if artifact_store is not None:
                try:
                    value = compact_result(artifact_store, name, value)
                except ArtifactError as error:
                    value = _bounded_tool_value(value, error)
            if len(json.dumps(value).encode()) > MAX_RESULT_BYTES:
                if artifact_store is not None:
                    # Pathological result (many individually-small strings):
                    # keep the complete structure in one JSON artifact.
                    try:
                        value = compact_result(artifact_store, name + "_result",
                                               json.dumps(value, ensure_ascii=False))
                    except ArtifactError as error:
                        value = _bounded_tool_value(value, error)
                else:
                    value = _bounded_tool_value(value)
        except (ValueError, OSError, UnicodeError) as error:
            value = {"error": str(error)}
        except Exception as error:
            trace({**identity, "state": "cancelled" if isinstance(error, Cancelled) else "failed",
                       "seconds": time.monotonic() - began,
                   "partial_results": getattr(error, "partial_results", [])})
            raise
        failed = isinstance(value, dict) and (bool(value.get("error")) or
                                              "exit_code" in value and value["exit_code"] != 0)
        trace({**identity, "state": "failed" if failed else "completed",
               "seconds": time.monotonic() - began, "output_bytes": len(json.dumps(value).encode()),
               "truncated": isinstance(value, dict) and value.get("truncated") is True})
        tools.check_cancelled()
        return call_id, name, value
    if len(calls) > 1 and tools.remaining() > 5 and all(
            name in {"read_file", "search", "list_files", "read_artifact", "search_artifact"}
            for _, name, _ in calls):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(calls)), thread_name_prefix="rsi-read") as pool:
            return list(pool.map(lambda call: execute(call, True), calls))
    return [execute(call) for call in calls]


def _has_artifact_reference(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return False
    return contains_artifact_reference(value)


class Conversation:
    def __init__(self, protocol, model, prompt, tools, effort, output_tokens=None, request_output_tokens=None):
        self.protocol, self.model, self.tools, self.effort = protocol, model, tools, effort
        self.output_tokens = output_tokens
        self.request_output_tokens = request_output_tokens
        self.history = [{"role": "user", "content": prompt}]
        if protocol == "gemini":
            self.history = [{"role": "user", "parts": [{"text": prompt}]}]

    def max_request_tokens(self):
        """单次请求的输出预算：不超过模型能力，也不超过策略上限。"""
        budget = self.request_output_tokens
        if type(budget) is not int or budget <= 0:
            budget = request_output_budget(self.model, "anthropic", self.output_tokens)
        if self.output_tokens:
            budget = min(budget, self.output_tokens)
        return max(1, budget)

    def request(self):
        p = self.protocol
        if self.effort != "default" and p not in ["responses", "chat_completions"]:
            raise RuntimeError("This native protocol requires effort=default; vendor reasoning settings are not silently translated")
        if p == "responses":
            body = {"model": self.model, "input": self.history, "store": False,
                    "tools": [{"type": "function", "name": n, "description": d, "parameters": s} for n, d, s in self.tools]}
            if self.output_tokens:
                body["max_output_tokens"] = self.output_tokens
            if self.effort != "default":
                body["reasoning"] = {"effort": self.effort}
                body["include"] = ["reasoning.encrypted_content"]
            return "/responses", body
        if p in ["chat_completions", "ollama"]:
            body = {"model": self.model, "messages": self.history,
                    "tools": [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, d, s in self.tools]}
            if self.output_tokens:
                # o-series rejects max_tokens on Chat Completions.
                key = ("max_completion_tokens" if re.search(r"\bo[134]\b", self.model.lower())
                       else "max_tokens")
                body[key] = self.output_tokens
            if self.effort != "default":
                body["reasoning_effort"] = self.effort
            if p == "ollama":
                body["stream"] = False
                if self.output_tokens:
                    body["options"] = {"num_predict": self.output_tokens}
            return ("/api/chat" if p == "ollama" else "/chat/completions"), body
        if p == "anthropic":
            max_tokens = self.max_request_tokens()
            return "/messages", {"model": self.model, "messages": self.history, "max_tokens": max_tokens,
                "tools": [{"name": n, "description": d, "input_schema": s} for n, d, s in self.tools]}
        if p == "gemini":
            def schema(s):
                # Gemini's OpenAPI schema does not accept additionalProperties.
                return {k: ({n: schema(v) for n, v in value.items()} if k == "properties" else schema(value) if isinstance(value, dict) else value)
                        for k, value in s.items() if k != "additionalProperties"}
            return "/models/" + urllib.parse.quote(self.model.removeprefix("models/"), safe="-._") + ":generateContent", {
                "contents": self.history, "tools": [{"functionDeclarations": [{"name": n, "description": d,
                    **({"parameters": schema(s)} if s["properties"] else {})} for n, d, s in self.tools]}]}
        raise ValueError("Unknown native protocol")

    def trim_history(self, max_bytes):
        """Bound oldest verbose tool outputs when approaching context capacity."""
        if len(self.history) <= 6:
            return
        try:
            encoded = len(json.dumps(self.history, ensure_ascii=False).encode("utf-8"))
        except (ValueError, TypeError):
            return
        if encoded < max_bytes * 0.7:
            return
        for item in self.history[1:-4]:
            if isinstance(item, dict):
                if (item.get("type") == "function_call_output" and
                        len(str(item.get("output", ""))) > 1000):
                    if _has_artifact_reference(item.get("output")):
                        # Rewriting an artifact envelope would make its opaque
                        # id unusable after context compression.
                        continue
                    item["output"] = json.dumps({"truncated": True, "excerpt": str(item["output"])[:500]})
                for part in item.get("parts", []):
                    response = part.get("functionResponse") if isinstance(part, dict) else None
                    if response and len(json.dumps(response.get("response", {}))) > 1000:
                        if _has_artifact_reference(response.get("response")):
                            continue
                        response["response"] = {"truncated": True, "excerpt": json.dumps(response["response"])[:500]}
                content = item.get("content")
                if isinstance(content, str) and len(content) > 1000:
                    if _has_artifact_reference(content):
                        continue
                    item["content"] = content[:500] + "\n...[output trimmed for context capacity]...\n" + content[-200:]
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "content" in part and len(str(part.get("content", ""))) > 1000:
                            if _has_artifact_reference(part.get("content")):
                                continue
                            part["content"] = str(part["content"])[:500] + "...[trimmed]"

    def receive(self, response):
        if not isinstance(response, dict):
            raise RuntimeError("Model API response must be an object")
        before = list(self.history)
        try:
            from .usage import native_usage
            details = native_usage(self.protocol, response)
            calls = self._receive(response)
            for call_id, name, _ in calls:
                if not isinstance(call_id, str) or not call_id or len(call_id) > 1024 or not isinstance(name, str) or not name:
                    raise ValueError("Invalid tool identity")
            return calls, details["tokens"] if details else 0
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            self.history = before
            raise RuntimeError("Model API returned an invalid tool response structure") from None
        except RuntimeError:
            self.history = before
            raise

    def _receive(self, response):
        p = self.protocol
        if p == "responses":
            output = response.get("output", [])
            self.history.extend(output)
            calls = [(x["call_id"], x["name"], parse_arguments(x["name"], x.get("arguments"))) for x in output if x.get("type") == "function_call"]
            return calls
        if p in ["chat_completions", "ollama"]:
            message = response["message"] if p == "ollama" else response["choices"][0]["message"]
            # Keep reasoning_content returned by gateways that require it on subsequent turns.
            self.history.append(message)
            calls = [(x.get("id", str(i)), x["function"]["name"], parse_arguments(x["function"]["name"], x["function"].get("arguments")))
                     for i, x in enumerate(message.get("tool_calls", []))]
            return calls
        if p == "anthropic":
            content = response.get("content", [])
            self.history.append({"role": "assistant", "content": content})
            calls = [(x["id"], x["name"], parse_arguments(x["name"], x.get("input"))) for x in content if x.get("type") == "tool_use"]
            return calls
        candidates = response.get("candidates") or []
        if not candidates:
            # prompt 被安全策略拦截时 candidates 为空；按可诊断错误处理而不是裸 IndexError。
            raise RuntimeError("Gemini returned no candidates; the request may have been blocked by safety settings")
        content = candidates[0]["content"]
        self.history.append(content)  # Preserve opaque thoughtSignature fields verbatim.
        calls = [(x["functionCall"].get("id", str(i)), x["functionCall"]["name"],
                  parse_arguments(x["functionCall"]["name"], x["functionCall"].get("args", {})))
                 for i, x in enumerate(content["parts"]) if "functionCall" in x]
        return calls

    def results(self, results):
        if self.protocol == "responses":
            self.history.extend({"type": "function_call_output", "call_id": call, "output": json.dumps(value, ensure_ascii=False)} for call, _, value in results)
        elif self.protocol in ["chat_completions", "ollama"]:
            self.history.extend({"role": "tool", **({"tool_name": name} if self.protocol == "ollama" else {"tool_call_id": call}),
                                 "content": json.dumps(value, ensure_ascii=False)} for call, name, value in results)
        elif self.protocol == "anthropic":
            self.history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": json.dumps(value, ensure_ascii=False)} for call, _, value in results]})
        else:
            self.history.append({"role": "user", "parts": [{"functionResponse": {"name": name, "response": {"result": value}, **({"id": call} if call else {})}}
                                                             for call, name, value in results]})


class NativeAgent:
    def __init__(self, store):
        self.store = store

    def invoke(self, role, prompt, workspace, project, run, config, route, provider, cancel=None, protected=()):
        protocol = provider["protocol"]
        key = Credentials(self.store.root).get(provider)
        headers = {"User-Agent": "Adaptive-RSI"}
        if key:
            headers[{"anthropic": "x-api-key", "gemini": "x-goog-api-key"}.get(protocol, "Authorization")] = key if protocol in ["anthropic", "gemini"] else "Bearer " + key
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        invocation = uuid.uuid4().hex
        tools = WorkspaceTools(workspace, role, self.store.root, config, cancel, protected,
                               run=run, invocation=invocation,
                               artifact_root=self.store.root / "runs" / str(run) / "artifacts")
        if role == "implementation":
            plan_path = Path(workspace).resolve().parent / "tool-scope.json"
            if plan_path.is_file():
                scope = json.loads(plan_path.read_text(encoding="utf-8"))
                if not isinstance(scope, list) or not scope:
                    raise RuntimeError("Approved native tool scope is invalid")
                tools.scope = [relative_path(value) for value in scope]
        from .store import credential_generation, provider_identity
        identity = provider_identity(provider, credential_generation(self.store.root, provider))
        directory, _ = self.store.catalog_entry(project, provider["id"], identity,
                                                 provider_identity(provider))
        entries = directory.get("models", []) if isinstance(directory, dict) else []
        entry = next((item for item in entries if isinstance(item, dict) and
                      item.get("id") == route["model"]), {}) if isinstance(entries, list) else {}
        try:
            capabilities = model_capabilities(route["model"], protocol, entry.get("capability_override"))
        except (TypeError, ValueError):
            capabilities = model_capabilities(route["model"], protocol)
            capabilities["override_error"] = True
            try:
                self.store.event("capability_override_invalid", "Invalid model capability override; conservative defaults used",
                                 project, run, {"provider": provider["id"], "model": route["model"]})
            except (OSError, RuntimeError, sqlite3.Error):
                logging.getLogger(__name__).warning("Capability override diagnostic unavailable")
        conversation = Conversation(protocol, route["model"], prompt + "\nUse the provided tools to inspect the snapshot. End by calling finish with the requested structured result.", tools_for(role), route["effort"],
                                    output_tokens=capabilities["max_output_tokens"],
                                    request_output_tokens=capabilities["request_output_tokens"])
        began = time.monotonic()
        tools.deadline = began + config["limits"]["command_seconds"]
        max_bytes = min(config["limits"]["max_output_mb"] * 1024 * 1024,
                        capabilities["max_output_tokens"] * 4)
        context_bytes = min(64 * 1024 * 1024, capabilities["context_tokens"] * 4)
        # Probe calls answer quickly; other roles may reason for minutes on non-streaming APIs.
        # 实现/评审常触发长推理，300 秒曾让这类调用必然超时中断。
        timeout = 30 if role == "probe" else (900 if role == "implementation" else 600)
        seen_calls = set()
        # 命中输出上限的 finish 修复整次 call 只重试一次：同一上限下重复请求只会重复计费。
        output_limit_repairs = 0
        for turn in range(200):
            remaining = config["limits"]["command_seconds"] - (time.monotonic() - began)
            if remaining <= 0:
                raise ProviderUnavailable("Native conversation timed out", "timeout")
            if cancel and cancel():
                raise Cancelled("Model call cancelled")
            conversation.trim_history(context_bytes)
            suffix, payload = conversation.request()
            if len(json.dumps(payload).encode()) > context_bytes:
                raise RuntimeError("Native model input exceeds context capacity")
            call = self.store.reserve_call(project, run, role, route, config)
            started = time.monotonic()
            self.store.event("model", role + ": " + provider["id"] + " / " + route["model"], project, run)
            response = None
            try:
                request_args = (provider["base_url"].rstrip("/") + suffix, payload, headers, min(timeout, remaining), max_bytes)
                response = post_json(*request_args, **({"cancel": cancel} if cancel is not None else {}))
                request_id = getattr(response, "provider_request_id", None)
                if request_id:
                    self.store.event("model_response", "Model provider response received", project, run,
                                     {"call": call, "provider_request_id": request_id})
                # A malformed/blocked response can still contain billable usage.
                from .usage import native_usage
                self.store.record_usage(call, run, native_usage(protocol, response), time.monotonic() - started)
                reason = response_stop_reason(protocol, response)
                try:
                    calls, tokens = conversation.receive(response)
                except RuntimeError as exc:
                    exc.stop_reason = reason
                    raise
                if tokens < 0:
                    raise ValueError("Invalid token usage")
                if cancel and cancel():
                    raise Cancelled("Cancelled after model response")
                tools.remaining()
                if not calls:
                    error = RuntimeError("Model did not return supported tool calls; text-only models cannot execute this role")
                    error.code = "tool_format"
                    error.stop_reason = reason
                    raise error
                if len(calls) > 64:
                    raise RuntimeError("Model response exceeds 64 tool calls per turn")
                # Reject ambiguous batches before executing any potentially mutating
                # operation. Replaying one ID twice must never perform two writes.
                if len({call_id for call_id, _, _ in calls}) != len(calls):
                    raise RuntimeError("Model response contains duplicate tool call IDs")
                # Some gateways omit IDs; their positional fallbacks are only
                # meaningful within this turn, so track only explicit IDs.
                if protocol in ("responses", "anthropic"):
                    explicit_ids = {call_id for call_id, _, _ in calls}
                elif protocol in ("chat_completions", "ollama"):
                    message = response["message"] if protocol == "ollama" else response["choices"][0]["message"]
                    explicit_ids = {item["id"] for item in message.get("tool_calls", []) if item.get("id")}
                else:
                    parts = response["candidates"][0]["content"]["parts"]
                    explicit_ids = {part["functionCall"]["id"] for part in parts
                                    if part.get("functionCall", {}).get("id")}
                if explicit_ids & seen_calls:
                    raise RuntimeError("Model response reused an earlier tool call ID")
                seen_calls.update(explicit_ids)
                final = [x for x in calls if x[1] == "finish"]
                if final:
                    if len(calls) != 1:
                        conversation.results([(call_id, name, {"error": "finish must be called alone without other concurrent tool calls; please execute your other tool calls first and then call finish."}) for call_id, name, _ in calls])
                        self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
                        continue
                    call_id, name, args = final[0]
                    error = str(args) if isinstance(args, ArgumentsError) else None
                    if error is None:
                        try:
                            validate_output(args, SCHEMAS[role])
                        except RuntimeError as exc:
                            error = "finish arguments failed schema validation: " + str(exc)
                    if error is None:
                        self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
                        return args
                    if reason == "output_limit":
                        # 供应商明确说这次响应撞到输出上限：同样的预算再问一次只会再截断再计费。
                        # 整次 call 只放行一次缩短后重试（中间的工具轮不清零），之后按执行失败结束
                        # 并保留已记录的计费用量。
                        output_limit_repairs += 1
                        if output_limit_repairs > MAX_OUTPUT_LIMIT_REPAIRS:
                            problem = ModelFailure(
                                "Model output was truncated by the request output budget and the structured result stayed invalid; "
                                "shorten the result or split the work across several smaller finish calls instead of repeating the same request",
                                "tool_format")
                            problem.stop_reason = "output_limit"
                            raise problem
                        error += " (the response hit the output token limit; shorten the result or split it)"
                    conversation.results([(call_id, name, {"error": error})])
                    self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
                    continue
                tools.trace = lambda detail: self.store.event("native_tool", "Native tool lifecycle", project, run,
                                                             {"call": call, "turn": turn, "role": role, **detail})
                results = execute_tools(tools, calls)
                conversation.results(results)
                self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
            except ProviderUnavailable as exc:
                self.store.event("model_transport", "Model request retry safety", project, run,
                                 {"call": call, "request_outcome": exc.request_outcome,
                                  "possibly_billed": exc.possibly_billed, "retry_may_duplicate": exc.retry_may_duplicate})
                if getattr(exc, "provider_request_id", None):
                    self.store.event("model_response", "Model provider request failed", project, run,
                                     {"call": call, "provider_request_id": exc.provider_request_id})
                self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=?", (time.monotonic() - started, call))
                if tools.changed:
                    raise ProviderInterrupted(exc) from None
                raise
            except Exception as exc:
                if getattr(response, "provider_request_id", None):
                    exc.provider_request_id = response.provider_request_id
                if not getattr(exc, "failure_code", None) and not isinstance(exc, Cancelled):
                    exc.failure_code = "execution"
                    exc.code = "tool_format"
                self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=?", (time.monotonic() - started, call))
                raise
        raise RuntimeError("Native model exceeded the maximum tool-call turns")
