"""Public error allowlists and safe project configuration diagnostics."""
import json
import re
from pathlib import Path


# 只允许已知机器错误码离开服务端；异常文本可能包含路径、请求正文或凭据。
SAFE_ERROR_CODES = frozenset("""
unknown_provider invalid_provider invalid_mode invalid_key invalid_reference missing_key
keychain_unavailable keychain_locked keychain_failed credential_file_unavailable
builtin_provider provider_in_use update_in_progress connection_changed invalid_model
invalid_routes too_many_models unsupported_model_kind probe_failed default_effort_required
rate_limit auth connection unavailable unknown timeout too_large invalid_response
unsupported http_error provider_paused model_missing tool_format budget_exhausted
codex_missing codex_error opencode_missing opencode_error opencode_version_unknown
opencode_version_unsupported opencode_capability_missing invalid_credentials
invalid_backend invalid_auth_method invalid_auth_inputs invalid_auth_code invalid_auth_state
unknown_session auth_busy auth_credentials_busy service_closed process_interrupted
invalid_credential_registry credential_registry_full probe_busy catalog_busy
unsupported_auth_method invalid_api_key auth_protocol_error auth_timeout auth_expired
authorization_failed device_auth_unavailable auth_container_unavailable auth_worker_image_missing
""".split())


# 精确匹配固定业务校验文案；不使用前缀匹配，也不携带用户提交的字段或值。
SAFE_VALIDATION_MESSAGES = frozenset("""
Cancellation impact exceeds 1000 tasks; split the operation
Cancellation impact is truncated; cancel only the current task
Cascade cancellation must be a boolean
Preview dependent cancellations before confirming
Cancellation preview changed; review it again
Intervention reason must be at most 2000 characters
Task revision not found
Invalid revision pagination
Configuration changed in another window; reload before saving
Invalid command timeout: command_seconds must be 60..86400
provider_retry_minutes must be 0..1440
Invalid deployment health retry policy
Dependency task cannot reach merge; retry or choose another task
paused must be a boolean
enabled must be a boolean
Too many model providers
Provider requires id, name, base_url, env_key and optional protocol; never store API keys
Provider fields must be strings
Invalid, duplicate or reserved provider id
Provider URL requires HTTPS (HTTP allowed on loopback), without credentials or query
Provider requires a short display name
env_key must be an environment variable name, not a secret
Unsupported model protocol
Unsupported credential mode
Stored credentials require a reference, not an environment variable or key
Unexpected credential reference
Nested fallback routes are not supported
OpenCode model requires provider/model
Luna supports max, not ultra
Review blocking threshold must be low, medium or high
Triage policy requires 1..4000 characters
Invalid update mode or interval (minimum 60 minutes)
Automatic update policy must be patch, minor or stable
light_minutes must be between 5 and 525600
issues_minutes must be between 5 and 525600
deep_minutes must be between 5 and 525600
working_hours requires different start/end hours in 0..24; start must be below 24
Maximum concurrency is 4
Unsupported sandbox
Invalid container image
Sandbox limits must be positive
Repository must be owner/name
max_issues must be 1..1000
Invalid issue labels
Project weight must be 1..10
Automatic merge requires PR delivery
Automation workflow must be a fixed .github/workflows YAML file
Automation requires a fixed trusted tag
Automation requires the trusted workflow tag's full commit SHA
Release/deploy requires an explicit project and a pinned workflow tag
Deployment health URL requires HTTPS without credentials, query or fragment
Automatic deployment rollback requires an explicit health URL
Workflow repository must match the selected project's origin
Workflow must be an existing repository file
Feedback interval must be at least 5 minutes
Invalid base branch
No GitHub repository configured
Seed a dependency directory, not the entire repository
Invalid dependency snapshot size limit
Check must contain name, cwd, argv
Check requires a name and argv array
Invalid check arguments
Invalid evolution cost ratio
Invalid relative path
Path must remain inside the project
Select the root of an existing Git repository
Expected a GitHub repository URL
Use a GitHub repository URL such as https://github.com/owner/repository
Import directory already exists; register it as a local project instead
Project not found
Project and controller state must be in separate directory trees
Projects must not contain each other's directories
Goal must contain 1..30000 characters
Wait until active runs finish before importing projects
Wait for the active run to finish stopping before retrying or supplementing this task
Wait until active runs finish before registering projects so every worker uses the same isolation boundaries
Only project scan settings can inherit global defaults
An inherited setting cannot also have an override
Task overrides cannot grant delivery permissions
Invalid submission ID
Unknown inspection type
This task cannot be retried
Supplement must contain 1..10000 characters
Pause or finish active work before supplementing this task
Combined requirements exceed 30000 characters
Task not found
This task has finished and can no longer be cancelled
Submission ID was already used with different content
Candidate not found
Only a verified ready candidate can be promoted
Candidate evidence is incompatible with current configuration
Candidate content is incomplete
Unknown candidate kind
Task dependency cycle
Dependency must be another task in the same project
Urgency must be 0 or 1
Only waiting tasks can be scheduled
Automatic delivery must be explicitly enabled per project
Parallel project limit is a global setting; select global defaults
Software updates are a global setting
Only a verified candidate awaiting approval can be delivered
Independent gates have not passed
No verified code candidate to revert
Explicit confirmation is required to reset invalid project settings
Project configuration is valid; reset is unavailable
Project configuration must be a JSON object
Configuration must be an object
""".strip().splitlines())


class RequestError(ValueError):
    """本模块固定的用户提示；其余异常一律通过安全错误边界处理。"""


def project_display_name(name, path):
    """旧记录中的空名称也回退到目录名，避免界面出现空白项目。"""
    return name.strip() if isinstance(name, str) and name.strip() else Path(path).name


def configuration_error(exc, raw_config):
    """保留可修复的配置诊断，绝不返回底层异常文本。"""
    if isinstance(exc, json.JSONDecodeError):
        return "Project configuration is invalid: Expecting value (invalid JSON)"
    try:
        values = json.loads(raw_config or "{}")
        timezone = values.get("schedule", {}).get("timezone")
    except (ValueError, TypeError, AttributeError):
        timezone = None
    # 仅回显配置中显式设置的短时区名称；路径、URL 和凭据不能匹配此格式。
    from zoneinfo import ZoneInfoNotFoundError
    if isinstance(exc, ZoneInfoNotFoundError) and isinstance(timezone, str) and re.fullmatch(
            r"[A-Za-z_]{1,32}/[A-Za-z_]{1,32}(?:/[A-Za-z_]{1,32})?", timezone):
        return "Project configuration is invalid: Unknown timezone " + timezone
    if isinstance(exc, ValueError) and str(exc) in SAFE_VALIDATION_MESSAGES:
        return "Project configuration is invalid: " + str(exc)
    return "Project configuration is invalid; check field names, types, model routes and timezone"
