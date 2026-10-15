# R92 / P5 完成核对（2026-10-15）

P5 摄取组 23 文件 323/0/0/0（163.742 秒），补充六文件 79/0/0/0（74.998 秒），最后启动/关闭四文件 50/0/0/0（14.812 秒）；按节点取最新共 447 个通过，非一次整组运行；2465 collected，仅收集。

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


本地提交核实：`2ea49f2`，73 个已提交文件与隔离验证树逐 blob SHA256 一致。

## 验收前调查记录（历史，未验收措辞已由以上结论替代）

# R92 / P5 要求核对工作表（进行中，非验收结论）

2026-10-15。依据已批准方案 §8.3.G 和处理计划 P5；当前 rc7，031–036 未冻结。本表用于把源码、已运行测试和未解决问题分开，不能据此宣布 P5 完成。下表保留逐轮审计过程，最新验证和升级/回退边界见文末；完整目标 P3–P12 继续 active。

## 合同与当前证据

| 要求 | 当前源码入口与测试证据 | 本次核对结论 / 待补 |
|---|---|---|
| 一张附加表、唯一规范键、版本/代次/租约，秘密不进入附加 JSON | 031_ingest_entries.sql；candidates/identity.py、entries.py；test_ingest_identity、test_ingest_entries | 已读规范键和登记源码；字段/约束在 031。最终验收还需本阶段完整定向运行，不能只引用收集数 |
| 画廊优先、频道原帖/转发等价、普通群/私聊不跨账号猜测 | ingest_key、message_key、Bot/MTProto parser；test_ingest_identity | 同一函数，Token/文件 ID 不参与键；现存正式用例覆盖可靠与不可靠坐标。本轮监听改造没有改变规则 |
| 已入库与未入库同一匹配入口，先领取再 AI，重复只补来源 | process_message → prepare_observation → register_observation/claim_entry；test_ingest_pipeline | 本轮十一文件组包含 pipeline，154/0。首输入固定、并发期间无候选、第二账号来源保留的合同已运行 |
| 新来源也受订阅/解析/附件规则校验，现有审批/人工字段不被重复覆盖 | process_message 本地检查、prepare_observation/reselect_pending_input/source_still_eligible；pipeline/source_rules | 已复现修复失败输入/订阅失效后的替代选择；合格原输入不变，新/既有合格观察可恢复。AI 前与提交事务重查来源，避免只凭接收时检查；最新运行见 progress，完整 P5 其他要求仍未验收 |
| 各账号观察/附件保留真实归属，未知历史不猜账号 | entries.observing_account、source_messages、031/033；test_multi_account_sources、test_ingest_migration、test_legacy_source_subscriptions | 分别在本轮 154/0 与 102/0 组；033 只复制旧配置，不改历史观察身份 |
| 消息先可靠持久化再推进接收游标，慢 AI 不影响接收 | Bot raw inbox、034/TelegramUserInbox、AccountListener 独立消费；test_listener_delivery | 最终 59/0 组三文件含十八个 delivery 合同；新任务没有另一套业务去重/下载调度器。P9 持久冷却与跨功能预算仍待后续 |
| 有界重试、失联恢复、旧 owner 不可晚提交，发布无半成品 | claim_entry/renew/check/fail、_execute_claim、publish_candidate；entries/pipeline/manual/generations | 本轮覆盖 pipeline/generations。正式 manual/entries 用例需在最终验收时按最新文件统一取证；旧 run 不冒充本轮全量 |
| 自动判定、自动入队、审计与摄取后续意图持久、通知丢失可恢复 | 031 followup triggers、followups.py、AutoApprovalSweeper、auto_approval/commit.py | 既有 test_ingest_followups/test_auto_approval_atomicity；本轮包含 auto_approval_workflow，但不据此替代前两文件的最终验收 |
| 手动加入同键预留；候选/元数据/封面/种子/关联原子发布 | ingest_manual_link、publish_manual_gallery、Database.insert_manual_candidate | 源码可见先 claim，再验证，再同事务发布；test_manual_ingest_identity 需列入最终组。手动发布后的 enqueue 仍是事务外调用，见下方 P8 边界 |
| 信息补全/纠错、转发查回、身份合并撤销旧处理并保留历史 | resolve_message_identity、references.py、merging.py、changes.py | 本轮 source_stops/generations 已跑；references/interleavings/operations 的全合同仍需汇总最新证据 |
| 编辑/移除/下载停止真实收束，另一账号观察不连坐，已移除不复活 | changes.py、queue operation、entry generation；test_ingest_source_stops/test_ingest_generations | 本轮 154/0 包含这两文件；不能据此宣布 P6 合格备用账号/串行新 attempt 完成 |
| 媒体组/回复只作补证，不覆盖确定冲突的画廊 | persistence._related_candidate、references；test_ingest_pipeline/test_ingest_source_stops | 本轮对应组已跑；保留源的审核、产物与独立任务由已有 merge 合同复核 |
| 旧库回填/单列 gid 唯一，歧义整笔回滚，不伪造历史处理 | migration.py、Database.initialize、031；test_ingest_migration | 本轮 154/0 含迁移文件；还需正式升级/回退说明与阶段结束的完整映射 |
| 所有写入口复用核心，无旁路候选/来源写入 | 下面的旧写入口清单 | 已执行当前 app/*.py SQL 和调用搜索并读实际函数；仍需把相关引用路径与最终定向结果纳入验收 |

## 当前写入口清单（2026-10-15 实际源码搜索）

搜索范围仅 app 下 Python，匹配候选/观察/关联插入及画廊更新，并结合 CodeGraph 调用定位；没有读取真实私有配置。

- 新候选插入只有 candidates/persistence.py 的 publish_candidate，以及 db/database.py 的 insert_manual_candidate。后者在已有写事务内领取或核对同一 gallery claim，再 finish_claim；非画廊的历史内部入口不伪造等价画廊身份。
- 新 source_messages 插入集中在 entries.register_observation；candidate_messages 插入位于该登记、finish_claim、persistence 的受控编辑、merging 和 references。它们是同一身份变更/发布链，不能仅因出现多处 SQL 就算作独立摄取器。
- Database.save_candidate_message 是兼容的“已判定输入”适配，转入 save_decided_message → prepare_observation；本次 app 搜索没有产品调用者，现存直接调用在 tests。此入口已有来源停止合同，仍需最终审查它是否应继续作为内部兼容 API。
- ex_gid 的写者集中在 persistence、merging、references 和 migration；画廊更正不再由页面/API 直接写候选。要继续用调用图核对这些入口的实际条件，而非只按 SQL 搜索宣称安全。
- Bot/MTProto 接收现在仅保存投递；它们没有直接插候选。共享 CandidateIngestor 执行规范化、领取及最终发布，独立 listener 的差异只有账号上下文和传输输入。

## 需先处理或明确归属的边界

1. **固定输入、账号状态与过期 claim 已有直接合同**：source_still_eligible 在同一事务核对订阅/附件及注册账号的启用、删除、停止、当前凭据验证状态；仍合格的原输入只由其账号领取，其他账号不借用自己的 lease 处理它。原账号不可借用时，新/已保存的合格观察可接替，含已过期的旧 claim，旧 revision 不能提交。Database 绑定同一 IngestProcessors，TG/手动领取登记与本地任务注册在短锁内完成；本地请求及清理尚未退出时，不凭租约过期放行。恢复每轮仍有 100 条预算，共享每账号扫描游标，避免不属于本账号的固定输入反复占满前 100 条。test_ingest_account_recovery 十一条与 manual 两条补充合同；最新十一文件 146/0，详情见 progress。此处是摄取恢复，不能替代 P6 下载 attempt 接替，也不能替代 P9 worker_session/持久预算的完整验收。
2. **P8 流水线意图**：新的手动候选发布为 APPROVED 后，enqueue_manual_candidate 在事务外执行；031 的自动判定 followup trigger 只针对 PENDING_REVIEW/NEEDS_INFO，不会替手动加入声明可靠的待入队意图。P8 需显式覆盖“新手动发布成功、enqueue 前崩溃”的恢复；与“升级前历史 APPROVED 无任务不自动重下”分开，不能扫描所有已审批行来猜意图。
3. **P6 尚未完成**：原用户账号优先、可证明同一附件的备用账号、旧请求真正退出后新 attempt、Bot file_id 不串账号，需要完整交错合同及最终审查。
4. **最终运行与文档**：逐项核对后再跑覆盖整个 P5 的定向组，并冻结 031、更新阶段版本/基线/文档与本地提交；未满足前保持进行中。P6 的 032–034 不能被误报为已提交或作为 P5 完成的替代证据。

## 本轮旧写入口与停止审计（2026-10-15，覆盖上表待定位项）

- 实际重新搜索 `app/**/*.py` 的候选/观察/关联插入与画廊更新，并结合 CodeGraph 读调用链。候选新建仍只有 `publish_candidate` 和 `Database.insert_manual_candidate`；前者必须通过 `check_claim`，后者要求调用者持有事务并领取/核验相同画廊键。`ExHentaiService.publish_manual_gallery` 在同一事务写元数据、种子和封面。
- `admit_message` 在产品代码中只由 `_execute_claim` 调用；`Database.save_candidate_message` 没有产品调用者，保留给已经判定的内部兼容调用，并转入 `save_decided_message → prepare_observation → publish_candidate`。编辑先走 `defer_observation_change`，正式兼容写入口测试证明无法绕过停止；不再有直接先跑 AI、后各自插候选的产品入口。
- `register_observation` 是唯一新建 `source_messages` 的入口；`candidate_messages` 的其余写者属于领取完成、已保存来源编辑、受控合并和身份变更。`finish_claim` 核验唯一键/画廊对应及 owner/revision/generation，合并检查旧执行和未确认远端资源，人工/锁定字段和审核冲突使事务回滚。
- 本轮复现一个真实遗漏：停止匹配只认原始 `observation_id`，不认下载选中的 `download_observation_id`。备用消息被编辑/移除时，原来只挡最终提交，仍继续传输。现在来源变更与失败操作重试都匹配两个观察引用；真实用户下载的编辑/移除及操作重试三条失败已复现修复，等待物理清理后才应用变更。
- 初次完整 P5 审计组为十八文件 **248/0/0/0，137.821 秒**，之后增加备用观察停止和快照恢复合同。此组与其他组重叠，不累加；后续最新完整文件结果见 progress。阶段版本、冻结与提交仍需按 P5/P6 实际交付边界收口，不能把本审计记录当作 P7–P12 完成。
