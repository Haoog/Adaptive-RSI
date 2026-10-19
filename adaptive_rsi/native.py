"""Native API adapters with a host-controlled, workspace-only tool loop."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .agent import SCHEMAS, TEXT, object_schema, validate_output
from .config import relative_path
from .credentials import Credentials
from .process import Cancelled, Sandbox, scrub_tree


class ProviderUnavailable(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_json(url, payload, headers, timeout, max_bytes):
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise RuntimeError("Model response exceeds output limit")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Provider error bodies may echo authentication headers or private prompts.
        error = "Model API HTTP " + str(exc.code)
        if exc.code in [404, 429, 502, 503, 504]:
            raise ProviderUnavailable(error) from None
        raise RuntimeError(error + "; check endpoint, credentials and model capabilities") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderUnavailable("Model API connection failed or timed out") from None
    except ValueError:
        raise RuntimeError("Model API did not return valid JSON") from None


def tools_for(role):
    if role == "probe":
        return [("finish", "Return summary RSI_OK to test structured tool output.", SCHEMAS[role])]
    tools = [
        ("list_files", "List tracked and new source files in the snapshot, excluding internal metadata.", object_schema({})),
        ("read_file", "Read at most 500 lines. Paths must be relative to this snapshot.", object_schema({
            "path": TEXT, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}})),
        ("search", "Find literal text in source files. Results are bounded.", object_schema({"text": TEXT})),
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
    def __init__(self, workspace, role, state, config, cancel, protected):
        self.workspace = Path(workspace).resolve()
        self.role, self.state, self.config = role, state, config
        self.cancel, self.protected = cancel, protected
        self.changed = False

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
        result = []
        for root, dirs, files in os.walk(self.workspace, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != ".git" and not d.startswith(".rsi-")
                             and d not in ["node_modules", "__pycache__", ".venv"] and not (Path(root) / d).is_symlink())
            for name in sorted(files):
                path = Path(root) / name
                if not path.is_symlink() and not name.startswith(".rsi-"):
                    result.append(path.relative_to(self.workspace).as_posix())
                    if len(result) >= 4000:
                        return result
        return result

    def execute(self, name, args):
        if self.cancel and self.cancel():
            raise Cancelled("Cancelled before model tool execution")
        schema = next((schema for tool, _, schema in tools_for(self.role) if tool == name and tool != "finish"), None)
        if schema is None:
            raise ValueError("Tool not available for this role")
        validate_output(args, schema)
        if name == "list_files":
            return self.files()
        if name == "read_file":
            start, end = args["start_line"], args["end_line"]
            if not 1 <= start <= end <= start + 499:
                raise ValueError("Read requires 1-based range of at most 500 lines")
            path = self.path(args["path"])
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Source file is too large")
            return "\n".join(f"{i}: {line}" for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if start <= i <= end)
        if name == "search":
            if not args["text"] or len(args["text"]) > 1000:
                raise ValueError("Search requires 1..1000 characters")
            result = []
            for file in self.files():
                path = self.path(file)
                if path.stat().st_size > 1024 * 1024:
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except UnicodeError:
                    continue
                for i, line in enumerate(lines, 1):
                    if args["text"] in line:
                        result.append({"path": file, "line": i, "text": line[:1000]})
                        if len(result) >= 100:
                            return result
            return result
        if name in ["write_file", "replace_text"]:
            path = self.path(args["path"])
            if name == "replace_text":
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Source file is too large")
                text = path.read_text(encoding="utf-8")
                if not args["old"] or text.count(args["old"]) != 1:
                    raise ValueError("Replacement requires one unique match")
                text = text.replace(args["old"], args["new"], 1)
            else:
                text = args["content"]
            if len(text.encode()) > 1024 * 1024:
                raise ValueError("Source write exceeds 1 MiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.changed = True
            return {"written": args["path"]}
        if name == "run_command":
            relative_path(args["cwd"])
            if not args["argv"] or any("\0" in x or len(x) > 4000 for x in args["argv"]):
                raise ValueError("Invalid command arguments")
            self.changed = True
            sandbox = Sandbox(self.state, self.config)
            result = sandbox.check({"name": "model tool", **args}, self.workspace, self.cancel,
                                   hidden=self.protected)
            scrub_tree(self.workspace)
            return {k: result[k] for k in ["exit_code", "stdout", "stderr", "error"]}
        raise ValueError("Unknown tool")


class Conversation:
    def __init__(self, protocol, model, prompt, tools, effort):
        self.protocol, self.model, self.tools, self.effort = protocol, model, tools, effort
        self.history = [{"role": "user", "content": prompt}]
        if protocol == "gemini":
            self.history = [{"role": "user", "parts": [{"text": prompt}]}]

    def request(self):
        p = self.protocol
        if self.effort != "default" and p not in ["responses", "chat_completions"]:
            raise RuntimeError("This native protocol requires effort=default; vendor reasoning settings are not silently translated")
        if p == "responses":
            body = {"model": self.model, "input": self.history, "store": False,
                    "tools": [{"type": "function", "name": n, "description": d, "parameters": s} for n, d, s in self.tools]}
            if self.effort != "default":
                body["reasoning"] = {"effort": self.effort}
                body["include"] = ["reasoning.encrypted_content"]
            return "/responses", body
        if p in ["chat_completions", "ollama"]:
            body = {"model": self.model, "messages": self.history,
                    "tools": [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, d, s in self.tools]}
            if self.effort != "default":
                body["reasoning_effort"] = self.effort
            if p == "ollama":
                body["stream"] = False
            return ("/api/chat" if p == "ollama" else "/chat/completions"), body
        if p == "anthropic":
            return "/messages", {"model": self.model, "messages": self.history, "max_tokens": 8192,
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

    def receive(self, response):
        p = self.protocol
        if p == "responses":
            output = response.get("output", [])
            self.history.extend(output)
            usage = response.get("usage", {})
            calls = [(x["call_id"], x["name"], json.loads(x["arguments"])) for x in output if x.get("type") == "function_call"]
            return calls, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        if p in ["chat_completions", "ollama"]:
            message = response["message"] if p == "ollama" else response["choices"][0]["message"]
            # Keep reasoning_content returned by gateways that require it on subsequent turns.
            self.history.append(message)
            usage = response.get("usage", {})
            calls = [(x.get("id", str(i)), x["function"]["name"], x["function"]["arguments"] if isinstance(x["function"]["arguments"], dict) else json.loads(x["function"]["arguments"]))
                     for i, x in enumerate(message.get("tool_calls", []))]
            tokens = int(response.get("prompt_eval_count", 0)) + int(response.get("eval_count", 0)) if p == "ollama" else int(usage.get("total_tokens", 0))
            return calls, tokens
        if p == "anthropic":
            content = response.get("content", [])
            self.history.append({"role": "assistant", "content": content})
            usage = response.get("usage", {})
            calls = [(x["id"], x["name"], x["input"]) for x in content if x.get("type") == "tool_use"]
            return calls, sum(int(usage.get(k, 0)) for k in ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"])
        content = response["candidates"][0]["content"]
        self.history.append(content)  # Preserve opaque thoughtSignature fields verbatim.
        calls = [(x["functionCall"].get("id", str(i)), x["functionCall"]["name"], x["functionCall"].get("args", {}))
                 for i, x in enumerate(content["parts"]) if "functionCall" in x]
        return calls, int(response.get("usageMetadata", {}).get("totalTokenCount", 0))

    def results(self, results):
        if self.protocol == "responses":
            self.history.extend({"type": "function_call_output", "call_id": call, "output": json.dumps(value, ensure_ascii=False)} for call, _, value in results)
        elif self.protocol in ["chat_completions", "ollama"]:
            self.history.extend({"role": "tool", **({"tool_name": name} if self.protocol == "ollama" else {"tool_call_id": call}),
                                 "content": json.dumps(value, ensure_ascii=False)} for call, name, value in results)
        elif self.protocol == "anthropic":
            self.history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call, "content": json.dumps(value, ensure_ascii=False)} for call, _, value in results]})
        else:
            self.history.append({"role": "user", "parts": [{"functionResponse": {"name": name, "response": {"result": value}, **({"id": call} if not call.isdigit() else {})}}
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
        tools = WorkspaceTools(workspace, role, self.store.root, config, cancel, protected)
        conversation = Conversation(protocol, route["model"], prompt + "\nUse the provided tools to inspect the snapshot. End by calling finish with the requested structured result.", tools_for(role), route["effort"])
        began = time.monotonic()
        max_bytes = config["limits"]["max_output_mb"] * 1024 * 1024
        for _ in range(32):
            remaining = config["limits"]["run_seconds"] - (time.monotonic() - began)
            if cancel and cancel():
                raise Cancelled("Model call cancelled")
            if remaining <= 0:
                raise RuntimeError("Native model session timed out")
            suffix, payload = conversation.request()
            if len(json.dumps(payload).encode()) > max_bytes:
                raise RuntimeError("Native model context exceeds output budget")
            call = self.store.reserve_call(project, run, role, route, config)
            started = time.monotonic()
            self.store.event("model", role + ": " + provider["id"] + " / " + route["model"], project, run)
            try:
                response = post_json(provider["base_url"].rstrip("/") + suffix, payload, headers, min(30, remaining), max_bytes)
                calls, tokens = conversation.receive(response)
                if tokens < 0:
                    raise ValueError("Invalid token usage")
                self.store.execute("UPDATE calls SET tokens=?,seconds=? WHERE id=?", (tokens, time.monotonic() - started, call))
                self.store.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (tokens, run))
                if cancel and cancel():
                    raise Cancelled("Cancelled after model response")
                if not calls or len(calls) > 20:
                    raise RuntimeError("Model did not return supported tool calls; text-only models cannot execute this role")
                final = [args for _, name, args in calls if name == "finish"]
                if final:
                    if len(calls) != 1:
                        raise RuntimeError("finish must be the only tool call")
                    validate_output(final[0], SCHEMAS[role])
                    self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
                    return final[0]
                results = []
                for call_id, name, args in calls:
                    try:
                        value = tools.execute(name, args)
                        if len(json.dumps(value).encode()) > 128000:
                            value = {"error": "Tool output exceeds limit; narrow the request"}
                    except (ValueError, OSError, UnicodeError) as exc:
                        value = {"error": str(exc)}
                    results.append((call_id, name, value))
                conversation.results(results)
                self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
            except ProviderUnavailable:
                self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=?", (time.monotonic() - started, call))
                if tools.changed:
                    raise RuntimeError("Provider became unavailable after workspace tools; fallback refused to avoid mixing implementations") from None
                raise
            except Exception:
                self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=?", (time.monotonic() - started, call))
                raise
        raise RuntimeError("Native model tool-turn limit exceeded")
