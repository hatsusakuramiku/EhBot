# P12 最终验收核对（2026-10-16，未完成）

当前基线：P11 已提交 `22a29468397f3cdab76f643046c03035889c272f`，版本 `0.3.0rc14`，schema `039`。P12 Windows 检查点已提交供 Linux 接手；最终验收仍未完成。027–039 不修改。本表不代替实际测试结果。

## 首轮完整回归的 22 个失败

| 文件 | 失败数 | 定位与处理 |
|---|---:|---|
| `test_account_registry.py` | 1 | 后续迁移新增 recovery_count，旧 SELECT * 快照不再同宽。按旧列完整比较数据，另核对新增默认值。 |
| `test_downloaded_api_actions.py` | 6 | 旧请求缺少作品 UUID、版本和幂等键。通过公共快照读取后提交；缺失作品按共享命令返回 404。 |
| `test_ingest_followups.py` | 1 | 用新版 repository 向旧 schema 写夹具，缺少 rule_uuid。改用真实旧版 SQL 播种，仍验证升级、重启、一次判定与所有 alias 的确认。 |
| `test_logs_web.py` | 2 | 假登录字典绕过了新增会话验证，且应用未启动。启动真实应用并登录，使用有效会话直接驱动无限 SSE。 |
| `test_telegram_download_failover.py` | 2 | **产品缺陷**：retry/restart 将旧任务行改绑后，旧任务详情从该投影读不到错误码。无旧投影时从本任务最后一次 attempt 读取；完整历史快照断言保留。 |
| `test_telegraph_workflow.py` | 1 | 旧断言期待拒绝切源；当前批准合同为串行停止后切换。验证 STOPPING 时没有 B，处理停止后 A=CANCELLED/B=PENDING，重复请求仍只一个活动来源。 |
| `test_ui_shell.py` | 1 | **产品遗漏**：组件演示状态表没有 STOPPING。补枚举，保留全状态词表断言。 |
| `test_webui_operations_fix.py` | 1 | 原表单 WS 批量请求缺少作品前置条件。读取各作品版本后发送；仍核对两个不同 ID 和缺失 action 的校验。 |
| `test_review_batch.py` | 7 | 旧 FakeOrchestrator 未实现新的 review_batch 边界。改为检查原版本/key 转发、结构化拒绝、异常传播和只通知成功项。持久化规则由原有 batch/review 集成测试验证。 |

产品只改 `QueueControl._get_task_sync()` 的历史错误读取和组件库状态清单。未放宽 CAS、幂等、登录或来源互斥约束，未新增迁移。

## 已核实结果

| 验证 | 结果 |
|---|---|
| 上表九个完整文件 | **141 passed，172.43s，exit 0** |
| `test_work_review_batches.py`、`test_work_review_commands.py`、`test_work_review_http.py`、`test_execution_identity.py`、`test_v2_tasks.py` | **39 passed，38.60s，exit 0** |
| 当前完整收集 | **3093 tests collected，exit 0** |
| `compileall`、CodeGraph sync、`git diff --check` | 通过 |
| 最终 Windows 完整回归 | **3093 passed，2040.33s，exit 0**；JUnit 3093 tests / 0 failures / 0 errors / 0 skipped，time 2040.203s；session 76976 已退出 |

首轮通过的用例与定向结果不能相加冒充本轮完整通过数。此前 P12 接续记录另有迁移/恢复 25 条、HTTP/WS/v2、CBZ/取消、DOM root/proxy 通过；这些是前序结果，本轮没有重新执行浏览器脚本。

## Linux 验收缺口

**目标状态：blocked（2026-10-17）。** 相同环境阻塞已经连续复核，验收入口仍返回 exit 2，且没有正在运行的 Linux 验收进程。P3–P12 总范围保持不变；P12 尚未最终完成，但 Windows 检查点已提交供接手。恢复条件为可用的本机 Docker/Linux 或明确授权且可访问的现成 Linux 环境。恢复后执行下列真实 Linux 验收、核对结果并完成最终阶段文档和提交；不重复已完成的 Windows 全量验收。

本机没有 Docker CLI/engine；重新实跑 `python scripts/verify_docker_linux.py --offline --suite` 返回 **exit 2**、`docker command not found`，与脚本 `shutil.which("docker") is None` 分支一致。先前 follow-up 的 exit 1 记录有误，以本次直接保留 `$LASTEXITCODE` 的结果为准。Podman 不可用；WSL `--list --quiet` 返回 exit 1 和帮助文本，没有可运行的发行版；当前工具没有远程 Linux 执行入口。该脚本的 `--offline` 是工具链断网检查，不能当成 POSIX guardian 验收。

需要在真实 Linux 执行：

- `tests/integration/test_worker_sessions.py::test_hard_backend_exit_stops_archive_tool_and_descendant`：后台硬退出后工具及后代停止，进程锁释放。
- `tests/integration/test_archive_process_cancellation.py`：普通调用、探测及取消等待真实子进程退出。
- 真实 Linux 7-Zip 工具链和断网失败关闭检查；现有入口 `python scripts/verify_docker_linux.py --offline --suite` 可一并运行 Linux 完整套件。

Windows 上的这些测试只能覆盖 Windows 实现。静态编译不证明 Linux `/proc`、进程组、pidfd、flock 的运行行为。P12 保持未最终验收；当前检查点提交不等于阶段完成。不自行安装系统组件、不推送或部署。

## 接手与清理

完整回归进程已退出，JUnit 已核对，PHASES、progress、处理计划和 task_plan 已同步。接手从 Linux 验收开始；没有新改动时不重复 Windows 全量测试。仅清理已退出测试对应的具名 TEMP 目录，不建立 `.scratch/`。`app/downloads/models.py` 的既有 BOM 差异必须继续排除。

结果留痕后，已清理两个完整回归 TEMP 目录及九个具名 P12 文本/XML 报告，共 11 条精确路径。上文 TEMP stem 只用于说明已完成的运行，不再指向保留的文件；其他阶段 TEMP 未动。

依赖从本机已有 uv archive cache 通过 PYTHONPATH 加载；默认系统 Python 没有 pytest，不能将 `python -m pytest` 的导入失败误判为产品失败。默认 pytest 临时根有旧权限问题，必须显式指定任务独立 `--basetemp`。
