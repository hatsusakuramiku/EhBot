# R95 / P8 完成核对（2026-10-15）

rc11 / schema 037。2687 collected only；27 个相关文件的多组定向验证，按 classname/name 取最新结果共 590 个不同用例通过，0 failure/error/skip。没有运行整库、真实供应商或真实数据库升级。

| 要求 | 实现与正式合同 | 结论 |
|---|---|---|
| 明确产物版本和真实生产者 | 037 artifact_versions；历史 producer 为 null，内容/生产字段不可改，合并转归属、删除解除归属；artifact_pipeline/migration | 通过 |
| 重下并打包等待 B，关闭自动打包也保留明确意图 | queue_pack_intents 与重下命令同事务；B 完成后固定其产物；artifact_pipeline/archived_works | 通过 |
| 已明确排队 A 不被 B 偷换 | enqueue 捕获 UUID，worker 读取固定版本，缺失/内容不匹配明确失败；artifact_pipeline/conversion | 通过 |
| 通知丢失、入库后未 enqueue、重启可恢复 | 持久 READY 打包意图和 manual_download_intents，复用原 worker；artifact_pipeline/manual_ingest_identity/manual_add | 通过 |
| 自动设置在入队时捕获、重试/切源保留意图 | DownloadService/QueueControl/TorrentService；artifact_pipeline/torrent_workflow/queue_control | 通过 |
| CBZ 准备/替换/确认/清理四断点 | Publications journal、旧文件恢复副本、同事务产物/完成结果、延后原档清理；真实子进程 os._exit，artifact_pipeline | 通过 |
| 改名/refile 崩溃与路径钉一致 | queue_relocations，短事务移动/产物/路径钉/refile 完成结果；manual/worker 各三个真实退出断点，relocation_recovery/refile_cancellation | 通过 |
| refile 固定 CBZ，不悄悄追随后改文件 | CBZ UUID 入队快照与发布核对；打包/refile 不替换彼此活动输入；relocation_recovery | 通过 |
| 取消真实退出、旧文件保持、未知外部改动不覆盖 | 按 chunk/页面协作取消，7-Zip 原有进程树停止；未决发布先恢复再清理，未知内容保留；pack_cancellation/archive_process_cancellation/relocation_recovery | 通过 |
| 共享接口可指定输入并查询流水线 | v1 convert input_artifact_id 的类型/作品归属检查，task 增 pack_intent/artifacts/publications/relocations；api_domains/queue_control | 通过 |
| 旧 036 升级保留文件、未知历史、旧 pending 等 B | 实际旧 schema 升级合同；027–036 不改写，037 随阶段提交冻结 | 通过 |

最终主要组：relocate-final 九文件 211/0（23.947 秒），final 十九文件 263/0（145.871 秒），accept 五文件/类 107/0（28.341 秒）；最后普通回滚边界再核对移动两完整文件 15/0（5.467 秒）。另 providers 九文件 159/0、完整 api_domains 及其他相关节点纳入上述 590 个最终结果；组间重叠不相加，非一次最终整组运行。首次失败与修复详 progress 的 R95，TEMP 原始 XML 汇总后才清理。

边界：保障的是进程退出恢复，不承诺掉电/损坏磁盘的持久性；未知文件需要管理员核实。普通改名沿原行为在库内原子移动，跨挂载点 rename 失败会保留原文件；打包使用目标卷 staging，避免跨卷替换问题。worker_session/持续心跳、周期恢复和预算接 P9；全域 v2/CAS/幂等接 P10；ComicInfo 写锁与缩略图等接 P11；最终合同接 P12，不把这些范围写成已交付。

升级/回退沿 R92：先停服务，备份完整 SQLite/私有配置/配套文件，在隔离副本通过 Database.initialize 升级；回退恢复完整旧快照与配套源码，不删迁移记录降级。不能在实验性旧 037 上重复 initialize 期待补表。README/USAGE/REALTIME_API/方案/计划/PHASES/task_plan/findings/progress/AGENTS 同步；无环境变量变化。

验收后立即精确暂存本阶段代码、正式测试与文档，检查暂存差异、逐 blob SHA256 与已冻结迁移，再本地单行标题提交并核实 SHA。P8 提交前不进 P9；不推送、发布或部署。既有 models.py BOM 差异排除。

本地提交 **89ef4b938464f7eb92715bd21107d80f5fa850e0**（单行标题、无正文）；37 个提交 blobs 与提交前规范化内容逐 SHA256 相同，暂存为空，027–037 与提交一致；037 已冻结。提交留痕随下一阶段纳入，不另拆文档提交。
