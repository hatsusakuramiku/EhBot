# R90 / P3 要求核验

日期：2026-10-14。依据已批准处理计划 P3 与方案 §7。P3 已逐项核对并通过阶段定向验收，源码 rc6/schema 029，本地提交见 git log。P4–P12 的完整目标保持有效，未发布或部署。

## P3 要求与证据

| 要求 | 当前实现与正式回归 |
|---|---|
| 切源先检查 B，拒绝时 A 不变 | `QueueControl.command` 回执优先、写锁外预检、受理事务重验身份/版本/目标引用；真实 EH Cookie 画廊只读请求与 Telegraph 页面/页数检查。`test_source_preflight.py` 16 条覆盖目标未配置/失效、真实 A 继续、并发取消、引用变化、回执与 HTTP/Web/WS；EH 不申请收费归档 |
| A 实际退出前不启 B | `DownloadService.stop_attempt/process_operations`、`ConversionService.stop_attempt`；`test_queue_stopping.py` 用取消清理屏障证明停止未完成时槽不释放、B 无可领取任务；收尾失败回滚，重启读取原操作 |
| 所有预览子请求停止收束 | `TelegraphFetcher.fetch_all` 失败/超时/取消时 cancel 并 drain siblings；`test_telegraph_cancellation.py` 实际受控 HTTP；累计图片预算超限同样提前收束，不声称逐网络块硬预算 |
| 线程与子进程停止须确认 | `run_sync` 等真实线程退出，重复取消不提前返回；`test_pack_cancellation.py` 真实打包线程。7-Zip 普通调用与成员头探测使用 `managed_process`；Windows 暂停创建、Job 归属、进程树停止/退出检查，POSIX 独立组终止并回收；`test_archive_process_cancellation.py` 6 条实际进程测试；真实 7-Zip 26.02 的 27 条归档测试通过 |
| 旧执行不能完成、发布、改元数据或触发自动打包 | `execution_scope` 传播 task/attempt/generation；写事务提交核对当前身份，失败/停止意图使用独立核对事务；`test_execution_identity.py`、真实 provider 写入的 `test_provider_late_results.py`、打包线程合同。已结束 attempt 的原结果继续由 028 保持 |
| 远端推送先存意图，取消等待查回，归属/实例改变或失联保槽 | 029 `queue_remote_resources`；推送等待实际 HTTP 结果，确认归属与 delete 后 status；`test_remote_stop_confirmation.py` 覆盖推送竞态、未知回执、新共享资源拒绝/历史共享只解除、实例/归属变更、删除后仍存在；轮询登记进度/产物前核对本 attempt 的实例与标签，归属未知不能完成或启动 B；资源按 task_id 查询，不按可复用整数 job_id 误查 |
| 推送接受至 park 的断点不能直接再推送 | 租约回收对未确认资源或旧 hash 保存 restart/STOPPING；029 SQL 守卫拒绝旧写者直接回 PENDING；`test_restart_after_remote_push_before_park_stops_old_transfer_before_requeue` 的 INTENDED/REGISTERED 两种状态及直接写者守卫验证。停止未确认时不再次 add，停止后才产生新 task |
| 停止确认、新来源/重试受理与回执保持原子 | `QueueControl.finish_operation` 短事务核对持久操作，旧槽终止后入队；`test_queue_stopping.py` 的注入入队失败整笔回滚；命令回执竞争沿 P2 正式合同继续验证 |
| 运行记录允许移除，停止/清理不依赖已删除业务行 | 029 `queue_removals` 保存执行/远端/文件/目录清单，remove 接管旧 switch；`test_running_removal.py` 验证业务行立即删除、旧执行不重建、操作独立查询、默认保留已登记文件、显式删除等待实际停止 |
| 重加作品等待旧停止与文件清理，防整数 ID 复用 | 持久作品预留与认领/插入 SQL 守卫；画廊 ID 不含 token、纯磁链 hash 不分大小写；`test_running_removal.py` 验证新 UUID、同作品预留、candidate/job 整数复用、旧清理不改新 pin。P5 再整合统一 unique_key |
| 清理失败/重启可恢复，不误删共享或目录外文件 | attempt 目录输出前登记，文件清单同移除事务保存；清理拒绝根目录漂移/链接/其他产物引用，失败保持 PENDING；`test_running_removal.py` 的失败 unlink 后新服务重试、共享文件拒删、残片与既有产物分别处理 |
| 最小隔离暂存与文件提交屏障 | TG/EH/Telegraph/torrent 按 attempt 隔离；CBZ 在 library 同挂载 staging 生成，身份核对后发布；refile 在短写事务内核对、移动、登记 artifact 与 pin，普通 SQL 失败先恢复文件再释放写锁；`test_pack_cancellation.py` 与 `test_refile_cancellation.py`。完整发布崩溃恢复按 P8，不冒充已交付 |
| 共享入口与可查询收尾 | HTTP/Web/WS/批量任务命令复用核心；`GET /api/v1/removals/{id}` 独立身份、Cookie/Bearer、404，候选删除后仍可查；Web 提示后台停止与文件清理；`test_source_preflight.py`、`test_running_removal.py`、下载页面/API 回归 |
| 新库/旧库升级、重复 initialize 与迁移冲突回滚 | 新库由相关临时库夹具覆盖；028→029 保留运行历史、重复初始化；`test_upgrade_029_conflict_rolls_back_all_new_tables_and_guards` 验证冲突时整笔回滚、新表/守卫/版本号不残留，修正后可重试。027/028 不改写 |

## 六项原始触发条件映射

原证据来自 `.scratch/download_queue_review_repro.py`（2026-10-14 已清理）；旧脚本本身不作为新模型验收入口，正式回归复现其业务触发条件并调用真实持久化方法。

| 原触发 | 正式回归 |
|---|---|
| 同作品远端种子与本地原档同时活动 | `test_queue_control.py::test_same_work_different_sources_cannot_enqueue_together`；并发受理一个赢家，数据库唯一槽拒绝第二来源 |
| 推送取消被晚 park 覆盖 | `test_remote_stop_confirmation.py::test_cancel_during_push_waits_for_ack_then_queries_and_stops_owned_resource`；等待真实请求、查回归属、停止确认，无晚 park 复活 |
| 远端不可达却切 B | `test_queue_stopping.py::test_unconfirmed_remote_stop_keeps_slot_and_retries_durable_intent`；持久 STOPPING、B 不启动，新服务恢复同 operation |
| EH 返回后重建已移除 job | `test_provider_late_results.py::test_late_eh_persistence_cannot_finish_or_recreate_revoked_job`；实际 `_record_artifact_sync`，移除及取消两种状态，无 job/artifact/metadata 重建 |
| 旧种子完成覆盖取消或新任务 | `test_provider_late_results.py::test_late_torrent_completion_cannot_commit_to_cancelled_or_reused_job`；实际 `_complete_sync`，旧身份写入回滚，不覆盖复用整数编号的新 UUID |
| 预览一项失败后另一项继续下载 | `test_telegraph_cancellation.py::test_preview_requests_are_drained_before_route_returns`；failure/timeout/cancel 真实 HTTP 清理屏障，调用返回前 siblings 全部退出 |

## 版本、验收与后续范围

- 当前版本 rc6/029；相对 R89 净增 72，整库 2213 collected，仅收集；027/028 保持冻结，029 交付后冻结。
- 本轮先验：14 文件完整 315/0；六文件合同 52/0；原触发相关四文件 43/0；推送恢复相关四文件 69/0；真实 7-Zip 27/0；SOURCE_CHANGED HTTP 409 修正后预检文件 16/0。重叠计数不相加。
- 最终 29 个文件完整运行 625 passed / 0 failed / 0 skipped，290.819 秒；最后轮询归属检查修改后三文件复验 51/0，24.529 秒，按测试节点覆盖旧结果共 628 个不同用例最终通过。前一组在轮询检查修改前已导入模块，当前相关三个文件由后一组结果验证，不宣称单次全跑现行 29 文件。包含真实 7-Zip 27 条。文件清单见 progress 的 R90 条目。
- R90 文档/版本与本地阶段提交已收尾；尚未运行整库、真实 TG/EH/qBittorrent、代理或其他客户端，也没有 push/发布/部署或真实数据库升级。
- P4 接账号凭据版本；P5 统一键；P6 多 TG；P7 EH 池；P8 完整发布/精确产物；P9 心跳、快速重启与预算；P10 全域 API/并发；P11 其余审阅问题；P12 最终合同及本地联调。P3 定向验收不替代这些交付。

## 测试输出清理前核验（2026-10-14）

- 已结束的 29 文件验收：643 passed / 0 failed / 0 skipped，276.873 秒。该会话在共享种子拒绝修改前已导入模块，结果不代表修改后的当前代码全部通过。
- 共享种子拒绝修改后的三文件回归：47 条，36 passed / 11 failed / 0 skipped，87.164 秒。失败均在 `test_torrent_workflow.py`：轮询进度、停滞、单文件交付、摘要、目录打包、客户端错误、转换、队列进度、做种显示、自动打包、重复推送。部分旧夹具在受管推送前就设置远端已有种子，旧重复推送用例仍期待附着；须核对夹具时机并按方案验证拒绝共享资源，不能为通过测试恢复不受独占控制的附着行为。P3 仍未验收。
- 清理前核对其他报告：预检首验 97 条 / 1 失败，预检修正选测 13/0、最终完整文件 16/0；核心 315/0；进程 82/0；refile 135/0；推送恢复 69/0；合同 52/0；原始触发 43/0；迁移回滚 1/0；真实 7-Zip 27/0。一份误选路径报告为 0 执行。计数重叠，不相加冒充整库验收。
- 上述 XML、collect TXT 及 pytest 临时目录在测试进程退出、结果转入本文后清理；原始报告路径仅作为历史运行标识。原先保留的两个审阅复现脚本也已清理，正式回归位于 tests/；`.scratch/` 继续忽略，不再保留或重建。
- 后续修正已完成：受管下载夹具在 add 后才出现远端状态；预存在与 add 409 均拒绝共享附着；轮询确认实例/归属后才记进度或交付。最后 51/0 覆盖上述 11 条旧失败及新归属竞态，当前无未解决的 P3 测试失败。
