# R92 / P5 完成核对（2026-10-15）

P5 摄取组 23 文件 323/0/0/0（163.742 秒），补充六文件 79/0/0/0（74.998 秒），启动/关闭四文件 50/0/0/0（14.812 秒），最后含真实下载五文件 66/0/0/0（44.229 秒）；按节点取最新共 447 个通过，非一次整组运行；2465 collected，仅收集。

验收对象为提交中 rc8/031 独立树，P6/P7 混合工作区不是本阶段交付证据。每类仍启用一个账号。

| 要求 | 源码与正式合同 | 结论 |
|---|---|---|
| 一表/一规范键，已入库与未入库同一入口 | identity/entries/031；ingest_identity/entries/pipeline | 通过 |
| 频道原帖/转发等价，普通群私聊账号隔离，未知历史不猜 | Bot/MTProto parser、migration；identity/mtproto_translation/multi_account_sources | 通过 |
| 合格原输入固定，先领取再 AI，有界恢复与旧 owner 屏障 | ingestor/persistence/execution；pipeline/account_recovery/manual_ingest_identity | 通过 |
| 同键补来源，不重建、不重审、不覆盖人工字段 | persistence/merging；pipeline/references/migration | 通过 |
| 手动同键领取，候选/元数据/种子/封面原子发布 | manual/ExHentaiService.publish_manual_gallery；manual_ingest_identity/account_service | 通过 |
| 纠错/回复/移除代次/历史合并真实停止 | changes/references/QueueControl；generations/interleavings/source_stops/operations | 通过 |
| 自动审批、入队、审计原子；后续意图重启/interval=0 恢复 | followups/auto_approval/commit；followups/atomicity/workflow | 通过 |
| 031 同事务回填、gid 唯一、冲突整笔回滚、030 离线恢复 | Database.initialize/migration；ingest_migration/database | 通过 |
| 共享操作查询/版本重试，HTTP/Bearer/Web/WS 一致 | operations/ingestion/status/works；ingest_operations/api_v1/work_detail_web | 通过 |
| 真实单账号监听接入，旧共享订阅保留，观察归属正确 | ConnectionManager.process_message 适配、rule_source_id；ingest_legacy_runtime/connection_manager | 通过 |

P5 新候选仅由 publish_candidate 或 Database.insert_manual_candidate 受 claim/事务保护创建；观察登记集中在 register_observation，关联写入属于同一发布/变更/合并链；页面/API 不直接改 ex_gid。正式迁移要求通过 initialize 执行 Python 回填，不能只执行 SQL。

阶段边界：多账号专属来源/033 迁移、MTProto 收件箱/游标原子保存和接收/消费分离、下载备用账号独立 attempt 仍由 P6 验收；手动发布至入队崩溃意图属 P8，持久 worker_session/预算属 P9。没有以 P5 合同冒充整个方案 §8.3.G 的全部传输与跨进程合同完成。

真实下载补验：最终五文件 66/0/0/0（44.229 秒），显式来源账号 scope 与 Bot→用户单账号兼容通过。复现/修复及同阶段 amend 记录见 progress 的 R92 补记。

## 升级与回退

这是后续已验收版本的运维步骤，不表示当前未验收工作区可部署；本轮只在临时库演练，没有升级真实库。

1. 升级前停止 EhBot，并确认没有其他进程使用同一数据库/私有配置。保存旧源码或镜像版本，备份完整数据目录（含 SQLite 主文件及存在的 WAL/SHM、私有凭据/session），另行保留关联原档、CBZ 和工作目录。不要在运行时只复制 SQLite 主文件。
2. 在隔离副本上核对升级前版本和 `PRAGMA integrity_check` / `PRAGMA foreign_key_check`，再验证迁移。不要启动真实供应商监听、下载或远端控制来“试迁移”。031 需要 `Database.initialize` 中的 Python 回填与 SQL 同一事务，不能单独执行 031 SQL，不能手工插入迁移号来跳过回填。
3. 031 将同 gid、不同 token 的候选归并，并建立非空 gid 单列唯一索引。优先保留唯一活动任务的所有者，其次有产物的记录；保留任务/attempt 身份、产物路径、人工/锁定字段、审核、指定归档路径和合并审计。未知历史账号不猜测归属；旧 APPROVED 无任务不自动推断下载意图。
4. 两个活动所有者、未确认远端资源、人工字段/路径或审核结论冲突，以及来源/画廊身份歧义，都应先在旧版本核实处理。通过正常停止确认收束冲突执行，人工决定冲突字段/归属，再重新验证迁移。禁止手工把远端任务改成终态冒充停止。031 失败会回滚该迁移的 DDL、数据及版本记录，不撤销更早已提交的迁移。
5. 副本验证成功后，正式升级仍须停服务、保留原快照并按获准发布的完整版本启动；核对迁移记录、外键、同 gid 唯一性、候选/来源映射、任务/产物身份和历史审批。当前程序会按顺序执行所有未应用迁移，不提供任意跳过后续迁移的参数。
6. 回退时停止新版本，保存升级后现场用于核对，再恢复升级前完整数据/私有配置快照及配套旧源码/镜像，然后核对旧 schema 和历史记录。不要只降代码、只删除 `schema_migrations` 行或从合并审计拼凑“反向迁移”。快照后的新增记录和文件需单独核对，不能承诺自动保留；此步骤也不会撤销已经发生的供应商副作用。

正式回归 `test_offline_snapshot_restores_030_identity_and_history_after_upgrade` 演练了：030 两件历史作品及来源/任务 → 备份 → 升级合并并产生后续修改 → 离线恢复旧快照。恢复后按旧 schema 读取，证明两件作品、来源映射和任务身份恢复，完整性与外键检查通过。该测试证明本地 SQLite 快照边界，不冒充真实私有文件、远端副作用或整套部署回退实测。
