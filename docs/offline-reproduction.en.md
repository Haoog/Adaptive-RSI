# Sealed Experiment Bundle Offline Reproduction Checklist

[中文](offline-reproduction.md)

This checklist is for operators and auditors performing integrity verification and offline reproduction assessments of Adaptive RSI sealed experiment reproduction bundles in isolated, air-gapped, or credential-free environments.

## Overview and scope

Sealed experiment bundles exported through the strategy evolution subsystem (`Evolution.export_bundle`) bundle all hermetic local inputs required for reproduction:
- Candidate strategy or skill definitions (`candidate.json`)
- Historical experiment evidence and paired evaluation logs (`evidence.json`)
- Evaluation case specifications, repository baseline snapshots, and protected test overlays (`cases/`)
- Whole-input SHA-256 manifest and declared limitations (`bundle.json`)

Sealed bundles adhere to deterministic input and credential exclusion principles:
1. **Credential exclusion**: Bundles never include third-party API keys, private tokens, or environment credentials.
2. **Frozen inputs**: Source code repositories, base commit SHAs, and check commands are frozen at export time to prevent external drift.
3. **Isolation boundaries**: Input integrity verification establishes that files are untampered; it is not proof of standalone autonomous model execution in an air-gapped environment without external providers.

## Offline reproduction checklist

### Stage 1: Transfer and extraction

- [ ] **1.1 Obtain bundle**: Retrieve the sealed bundle directory exported by `Evolution.export_bundle`.
- [ ] **1.2 Place in isolated workspace**: Transfer the directory to the isolated host, ensuring the filesystem supports regular file reads and no escaping symlinks.
- [ ] **1.3 Check structure**: Confirm root files `bundle.json`, `candidate.json`, `evidence.json`, and the `cases/` directory are present.

### Stage 2: Input integrity verification

- [ ] **2.1 Execute hash verification**: Run `scripts/reproduce_bundle.py` or call `Evolution.verify_bundle(bundle_dir)`.
- [ ] **2.2 Compare manifest digests**: Ensure all files listed in `bundle.json` exist and match their SHA-256 digests.
- [ ] **2.3 Verify path safety**: Confirm no absolute paths, parent directory traversals (`..`), or symlinks escape the bundle tree.

### Stage 3: Host tools and execution dependencies

- [ ] **3.1 Check VCS tooling**: Verify `git` is available on the isolated host to operate on case repository snapshots.
- [ ] **3.2 Itemize check dependencies**: Scan commands in `cases/*/case.json` checks and verify all required binaries (such as `python3`, `node`, `pytest`, `cargo`).
- [ ] **3.3 Enumerate missing tools**: The reproduction script enumerates every missing dependency without aborting at the first failure, allowing comprehensive batch provisioning.

### Stage 4: Credentials and model access

- [ ] **4.1 Inspect model environment**: Check for required provider keys (such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).
- [ ] **4.2 Declare offline limitations**: When operating without credentials or network egress, verify that missing credentials and unreachable model APIs are explicitly enumerated in limitations without crashing.

### Stage 5: Offline execution and replay

- [ ] **5.1 Verify baseline checks**: With host tools available, run `--run-checks` to execute frozen checks against case repositories.
- [ ] **5.2 End-to-end evaluation note**: Re-running candidate generation requires provisioning appropriate model access and trusted network gateways.

## Reproduction script usage

The repository provides a dedicated script `scripts/reproduce_bundle.py` for safe inspection and dependency enumeration in credential-free environments.

### Command-line options

```sh
# Basic inspection: verify integrity and itemize dependencies and credentials
python3 -B scripts/reproduce_bundle.py /path/to/bundle

# Output machine-readable JSON
python3 -B scripts/reproduce_bundle.py /path/to/bundle --json

# Execute frozen checks when host tools are present
python3 -B scripts/reproduce_bundle.py /path/to/bundle --run-checks

# Strict mode: exit with code 2 if any tool or credential is missing
python3 -B scripts/reproduce_bundle.py /path/to/bundle --strict
```

- `bundle`: Path to sealed bundle directory (required).
- `--json`: Format result as JSON.
- `--run-checks`: Run case check commands if tools are present.
- `--strict`: Return exit code 2 when any dependency or credential is missing (default returns 0 upon completing inspection).

### Python API

You can call `Evolution.reproduce_bundle` directly:

```python
from adaptive_rsi.evolution import Evolution

report = Evolution.reproduce_bundle(
    "/path/to/bundle",
    env={},  # isolated environment dict
    run_checks=False
)

assert report["integrity"]["valid"] is True
print("Missing dependencies:", report["dependencies"]["missing"])
print("Missing credentials:", report["credentials"]["missing"])
print("Limitations:", report["limitations"])
```

### Typical output

Output on an air-gapped machine without credentials:

```text
=== 封存实验包离线复现检查 / Sealed Bundle Reproduction Inspection ===
[OK] 输入完整性校验 / Input Integrity: 通过 (All bundle files verified)

[-] 宿主工具与执行依赖 / Host Dependencies (1 可用, 1 缺失):
     [+] git: /usr/bin/git
     [!] pytest (cases: case-001): 缺失 (Command 'pytest' not found in PATH)

[-] 模型凭据与外部服务 / Credentials & Services (0 已配置, 1 缺失):
     [!] OPENAI_API_KEY: 缺失 (Credential environment variable 'OPENAI_API_KEY' is not set)

[-] 限制说明 / Limitations:
     1. Credentials are excluded; external services, model availability and host tools must be supplied independently
     2. Integrity verification is not proof of isolated-machine reproduction
     3. Missing credentials: OPENAI_API_KEY not configured; online model generation cannot be reproduced without external provider access
     4. Missing host tools: 1 required executable(s) missing (pytest)
     5. Isolated offline environment: external model APIs cannot be contacted without network egress and valid credentials

=== 结论 / Summary ===
输入完整性校验通过。缺凭据 (1 项) 或外部依赖 (1 项)，已逐项列出上述限制。
```

## Limitations and boundaries

1. **Credential protection**: Sealed bundles never leak credentials to offline or audit environments.
2. **Causal and generalization scope**: Input integrity proves artifacts were not altered; it does not guarantee automatic offline reproduction of stochastic model outputs.
3. **Non-blocking inspection**: Missing dependencies are enumerated across all cases rather than halting at the first failure.
