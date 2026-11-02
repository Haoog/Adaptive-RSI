# 封存实验包离线复现清单

[English](offline-reproduction.en.md)

本清单面向在隔离环境、无外网接入或无第三方 API 凭据机器上，对 Adaptive RSI 导出的封存实验包（Sealed Reproduction Bundle）进行完整性核验与复现评估的操作人员。

## 概述与适用范围

通过策略演化（Evolution）导出的封存实验包包含了复现评估所需的本地封闭输入：
- 候选策略/技能定义 (`candidate.json`)
- 历史实验记录与配对证据 (`evidence.json`)
- 评测用例配置、代码仓库基线快照与受保护覆盖层 (`cases/`)
- 全量输入 SHA-256 哈希清单与限制说明 (`bundle.json`)

封存实验包设计遵循确定性输入与凭据脱敏原则：
1. **脱敏保护**：包内绝不包含任何第三方 API Key、认证凭据或私有环境变量；`evidence.json` 仅记录非机密的 provider 需求（provider id 与所需环境变量名），不含密钥值。
2. **输入冻结**：所有用例的源码仓库、提交哈希与测试检查命令均在封存时固定，防范外部漂移。
3. **隔离边界**：输入完整性验证不等于真实环境推理，离线无凭据环境仅可核查输入完整性及离线可运行的本地用例，无法直接调用远端模型。

## 离线复现操作清单

### 阶段一：实验包传输与解压

- [ ] **1.1 获取封存包**：从已完成演化的实例或归档中获取实验包目录（例如由 `Evolution.export_bundle` 导出的目录）。
- [ ] **1.2 放置于隔离目录**：将包解压或拷贝至隔离机器的目标路径，确保所在文件系统支持常规文件读写且无越界软链接。
- [ ] **1.3 核对基础目录结构**：确认根目录下包含 `bundle.json`、`candidate.json`、`evidence.json` 以及 `cases/` 目录。

### 阶段二：输入完整性验证

- [ ] **2.1 执行哈希校验**：使用 `scripts/reproduce_bundle.py` 或 `Evolution.verify_bundle(bundle_dir)`。
- [ ] **2.2 比对文件清单**：确认 `bundle.json` 内 `files` 字段列出的所有文件均存在且 SHA-256 摘要完全一致。
- [ ] **2.3 路径安全性确认**：验证清单内无绝对路径、无 `..` 目录穿越、无软链接逃逸。

### 阶段三：宿主工具与执行依赖检查

- [ ] **3.1 检查版本控制工具**：确认隔离机器已安装 `git`，供用例仓库快照核对使用。
- [ ] **3.2 逐项列出检查项依赖**：仅针对 `bundle.json` 清单登记的 `cases/*/case.json` 中的 `checks` 命令列表，逐项排查所需可执行程序（例如 `python3`, `node`, `pytest`, `cargo` 等）；未登记的用例目录一律不读取、不执行，且会使完整性校验失败。
- [ ] **3.3 记录缺失依赖**：若隔离环境缺少某项工具，复现脚本会完整记录所有缺失项而非在首个缺失处直接中断，便于批量补齐离线运行时依赖。

### 阶段四：凭据与模型接入检查

- [ ] **4.1 检查模型环境变量**：凭据需求来自导出包记录的 provider 需求（`evidence.json` 的 `providers` 字段，仅含 provider id 与 `env_key` 环境变量名）。只有 `env_key` 指向环境变量的 provider 才需要凭据；登录授权或本地模型 provider（`env_key` 为空）不要求任何 API Key，也不会默认要求 `OPENAI_API_KEY`。旧版导出包若无此字段，脚本会输出「凭据可用性无法判定」的限制说明，而不是猜测密钥需求。
- [ ] **4.2 确认离线限制**：在无凭据或网络隔离环境下，脚本明确列出缺失的凭据项，并声明外部模型服务不可达的限制；不因缺少 API Key 导致程序异常崩溃。

### 阶段五：离线用例执行与重放

- [ ] **5.1 验证基线检查**：在宿主工具齐备前提下，运行 `--run-checks` 对用例执行固定检查项。命令运行在一次性临时副本中：先复制 `repository`，再将封存覆盖层 `overlay` 叠加到副本上（与评测器相同的符号链接拒绝语义），bundle 内原始文件保持不变；仅执行 manifest 登记用例的检查命令。
- [ ] **5.2 策略评估说明**：若需重新进行端到端候选评估，须在安全受信环境下补充对应模型凭据与网络代理配置。

## 复现脚本入口使用说明

仓库提供了专用复现入口脚本 `scripts/reproduce_bundle.py`，支持无凭据环境下的安全自检与逐项依赖枚举。

### 命令行参数

```sh
# 基础自检：验证完整性、逐项列出宿主依赖与凭据状态
python3 -B scripts/reproduce_bundle.py /path/to/bundle

# 输出机器可读的 JSON 结构
python3 -B scripts/reproduce_bundle.py /path/to/bundle --json

# 若本地依赖齐备，离线执行用例中的固定检查项
python3 -B scripts/reproduce_bundle.py /path/to/bundle --run-checks

# 严格模式：当缺少任何依赖或凭据时返回非零退出码 (exit code 2)
python3 -B scripts/reproduce_bundle.py /path/to/bundle --strict
```

- `bundle`：封存实验包路径（必填）。
- `--json`：以 JSON 格式打印完整检查结果。
- `--run-checks`：在工具齐备时对用例执行测试命令。
- `--strict`：若存在缺失的工具依赖或凭据，退出码为 2；默认模式下成功完成检查并列出缺失项即返回 0。

### Python API

可以在 Python 脚本或测试中直接调用 `Evolution.reproduce_bundle`：

```python
from adaptive_rsi.evolution import Evolution

# 检查离线复现条件与依赖清单
report = Evolution.reproduce_bundle(
    "/path/to/bundle",
    env={},  # 可传入隔离环境变量字典
    run_checks=False
)

assert report["integrity"]["valid"] is True
print("Missing dependencies:", report["dependencies"]["missing"])
print("Missing credentials:", report["credentials"]["missing"])
print("Limitations:", report["limitations"])
```

### 典型输出样例

在无凭据隔离机器上运行的典型输出如下：

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

## 限制与安全边界

1. **凭据安全**：导出包不含敏感信息，避免凭据泄漏到离线或第三方审计环境。
2. **因果与泛化界限**：输入完整性保证的是实验记录未被篡改，不代表离线离线重算能够全自动复现模型生成随机性。
3. **非阻断式排查**：依赖排查全面覆盖全部用例与配置，杜绝单点报错导致的隐蔽缺失。
