# R89 / P2 完成核验

日期：2026-10-14。目标：「持续工作，直到 R89 全部处理完成」。逐项核验及补漏完成：核心合同 28 条通过，相关 19 文件完整定向运行 460 passed / 0 failed / 0 skipped。

## 范围依据

核对批准后的处理计划（`cfa649d` 中的 P2 行）与当前方案：R89 对应 task/attempt、revision/generation、命令回执/operation、独立审批、旧整数 ID 映射和任务控制入口收口；验收门槛是同作品一个下载槽、意图与回执同事务、旧 provider 兼容适配受核心约束、既有 worker 原子认领。

原计划的 P3 已单独列出真实取消/切源/运行时移除、远端停止确认、旧 poller 屏障与最小 staging；P8/P9/P10 分别接续产物依赖、完整恢复和全域 API 收口。这些仍未实施，不以 R89 的合同通过宣称整个下载改造完成。

## 要求与当前证据

下表测试名均位于 `tests/integration/test_queue_control.py`；SQL 和服务实现一起核对，不仅依赖先前的完成描述。

| R89 要求 | 实现及验证证据 |
|---|---|
| 稳定 task 身份与旧整数兼容；终态重新开始保留历史 | 027 的 task 映射/restart adapter，`test_retry_new_task_and_attempt_preserves_history_and_rejects_old_draft`、`test_legacy_job_id_reuse_cannot_redirect_old_task_command`；旧 UUID 不能操作复用整数编号的新任务 |
| 真实领取才建立独立 attempt；下载和打包无双领取 | 两个既有 worker 的 `BEGIN IMMEDIATE`；`test_two_workers_claim_one_job_once`、`test_two_conversion_workers_claim_one_attempt`；每次竞争只认领一次 |
| 一个作品一个活动下载槽 | 027 部分唯一索引；`test_same_work_different_sources_cannot_enqueue_together`；内部重试的冲突也回业务错误，不改旧历史 |
| 客户端 observed revision 与执行 generation 分开 | QueueControl 同事务身份/版本检查；`test_different_devices_revision_competition_has_one_winner`、`test_task_progress_does_not_invalidate_priority_draft`；进度不使草稿失效，优先级不撤销执行 |
| 持久幂等、跨端重放、同键异内容拒绝、事务回滚 | 回执先于版本检查且作用域固定 administrator；`test_receipt_before_revision_check_and_key_content_conflict`、`test_simultaneous_same_key_submissions_share_one_operation`、`test_receipt_failure_rolls_back_state_intent_and_revision` |
| 回执有界保留，未结束操作的回执不提前清理 | `test_old_receipt_is_retained_until_its_operation_finishes`；八天旧回执在操作未结束时仍能重放，操作结束后按七天规则清理 |
| 独立审批，不凭任意历史任务推断 | `test_arbitrary_old_job_is_not_an_approval_fact`、`test_approval_survives_job_removal`、026 升级证据测试 |
| 取消/切源/远端重试先持久受理，不在裁决前做外部副作用 | `test_retry_refusal_performs_no_remote_side_effects`、`test_switch_source_persists_intent_without_enqueuing_second_source`；STOPPING 保留槽，未建立 B 的任务 |
| 独立收尾意图不随业务记录删除；旧写者不能提交停止后的结果 | `test_operation_survives_terminal_record_and_candidate_removal`、`test_stopping_keeps_slot_and_rejects_legacy_completion_and_artifact`；下载及打包 worker 将 SQL 守卫拒绝识别为旧结果拒绝 |
| 不直接移除活动记录；文件动作前检查所有来源 | `test_running_records_cannot_be_deleted_by_raw_legacy_writer`、`test_remove_checks_older_running_source_before_deleting_files`；P3 再实现持久运行清理意图 |
| HTTP/Web/WS 任务动作共用控制核心，无补最新版本的旁路 | `test_http_web_ws_share_preconditions_and_receipt_across_credentials`：实际临时 app、Cookie/Bearer/WS 重放相同结果，普通表单携带 UUID/版本/键；批量旧版本跳过、其余成功、重放返回原结果 |
| 迁移安全及重复启动 | 026→027→028、新库、027→028、重复 initialize、历史多活动来源使 027 整笔回滚；`test_upgrade_027_preserves_finished_attempt_and_is_idempotent` 等迁移合同 |

## 本次发现并修复的遗漏

1. 已结束的种子 attempt 在 retry/restart 受理为 STOPPING 时，被 027 adapter 改写状态及原错误。追加 **028_queue_attempt_history.sql** 冻结 finished_at 非空的执行事实；不修改已提交的 027，不伪造曾被旧逻辑丢失的历史。
2. `internal_command` 在作品槽占用时抛 SQLite 错误，导致内部重试/已下载重下无法得到与客户端相同的业务反馈。统一返回 WORK_SLOT_OCCUPIED，事务回滚保留旧历史。
3. 打包旧完成遭 SQL guard 拒绝后，worker 又尝试写 FAILED，再次触发守卫。按下载 worker 的相同错误集合识别旧提交拒绝，不把停止意图改成失败，也不创建额外尝试。

新增 7 个正式用例（核心合同 21→28），并加强原 HTTP/Web/WS 合同的结果一致性与批量混合裁决。最初 4 个复现失败已保留为 `.scratch/r89-audit-red.xml`；代码修复后核心相关 7 文件 154 条通过，28 条合同单独通过。

## 验证与交付状态

最终 19 文件完整定向运行 **460 passed / 0 failed / 0 skipped**（288.915 秒），证据 `.scratch/r89-audit-final.xml`：downloads、queue_control、conversion、torrent_workflow、archived_works、database、activity_web、activity_grouping、api_contracts、api_domains、work_detail_web、download_logging、conversion_logging、torrent_logging、telegraph_workflow、review_actions、auto_approval_workflow、archive_workflow、realtime。不是整库测试。

整库 **2141 collected**（仅收集，原 2134 + 7），证据 `.scratch/r89-audit-collect.txt`；最近整库全绿仍 R81–R84 的 2014。compileall、diff --check 与 CodeGraph sync 为提交前检查。原 R89 提交 7b8b297 保留，本次修正以本地 `fix(queue): close R89 history and command arbitration gaps` 提交，最终 SHA 见 git log。源码仍为未发布 0.3.0rc5，schema 028；不读取真实 private、不迁移真实库、不调用真实供应商、不推送或部署。

正式测试位于 tests；本地 XML/collect 证据位于已忽略的 .scratch，测试产生的临时数据库/文件目录在验收后清理。接手后继续 [处理计划](DOWNLOAD_QUEUE_AND_SHARED_API_IMPLEMENTATION_PLAN.md) 的 P3，无需重新审批或重复 R89。
