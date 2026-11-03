# 旧文件队列离线导入

`adaptive_rsi.legacy_queue.import_queue(store, project_id, snapshot_path)` 是显式调用的 Python 入口；不自动扫描、启动 worker 或切换旧系统。只对已停止变化的本地快照调用；调用方必须先确认旧 worker 停止，并核对 `.claim`、`.hold`、失败项、worktree 和已有 PR，避免双重领取。目标项目必须暂停且没有未结束运行。导入后项目仍保持暂停，恢复运行前仍需人工完成交接。

仅快照根目录的非隐藏普通 `.md` 文件可导入；符号链接和子目录不导入。`.claim`、`.hold`、`failed`、`done` 目录中的直接 Markdown 项在返回值 `reserved` 中列出，供人工核对，不转成任务。原文件不会移动或修改。Markdown 必须是非空 UTF-8，最多 30000 字符；否则整个批次失败。

任务使用现有 `goal` 来源和 `queued` 状态，证据包含规范化源路径、旧文件名及内容 SHA-256。按项目和规范化绝对路径去重：同一路径重跑返回 `existing`，内容变化会中止整个事务并要求人工处理；移动快照会改变身份，必须保持路径稳定。返回值 `imported` 和 `existing` 是任务 ID 列表。不会自动更新、删除或重新排队已导入任务，也不会跨目录或跨项目识别相同任务。源文件的排序不保证新调度器的执行顺序。

示例仅使用调用方新建的临时目录（`store` 是本地 Store，`project_id` 是已暂停项目）：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from adaptive_rsi.legacy_queue import import_queue

with TemporaryDirectory() as tmp:
    snapshot = Path(tmp) / 'snapshot'
    snapshot.mkdir()
    (snapshot / 'example.md').write_text('# Example\n', encoding='utf-8')
    result = import_queue(store, project_id, snapshot)
```

实际迁移需要稳定保留的本地快照路径；该示例只用于离线验证。此功能尚未连接旧脚本或服务器，也不验证 PR 或远端状态。
