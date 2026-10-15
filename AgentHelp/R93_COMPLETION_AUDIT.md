# R93 / P6 完成核对（2026-10-15）

独立 rc9/035 树 2596 collected（仅收集）；定向组按当前节点取最新 419 个不同用例通过，0 failure/error/skip，非一次整组或整库运行。 P5 基线 `2ea49f2`，本阶段 032–035，EH 池/036 未混入。

| 要求 | 生产入口与正式合同 | 结论 |
|---|---|---|
| 同类型多 Bot/用户启用，独立监听/session/缓存/挑战 | AccountService/AccountListener；telegram_account_runtime/account_service/multi_account_source_management | 通过 |
| 来源规则/游标按账号隔离，选中用户读取自己的会话 | settings/sources、source_commands；sources_web/telegram_user_web/multi_account_source_management | 通过 |
| 来源 CAS/回执/逐项批量、冲突草稿、HTTP/Bearer/WS/Web 共用 | 032、API/settings、realtime；source_commands/settings_write_api + DOM root/proxy | 通过 |
| 旧规则只迁移到真实导入槽，保留未知历史/游标/墓碑 | 033、AccountRegistry.import_legacy；legacy_source_subscriptions/ingest_legacy_runtime | 通过 |
| Bot inbox/offset 隔离；MTProto 消息/游标同事务 | 031/034、TelegramUserInbox；listener_delivery/multi_account_sources | 通过 |
| 慢 AI 不阻塞接收，来源/频道轮转，独立 FloodWait/失效 | ConnectionManager；listener_delivery，实际 DB 写线程/AI 清理等待 | 通过 |
| 原始 observation/账号优先，备用同文档、自身坐标/file_id | AccountService resolvers、DownloadService；source_account_downloads/telegram_file_identity | 通过 |
| 等真实退出后新 attempt，有限账号池/取消/重启/回滚 | 035、QueueControl/AccountLifecycle；telegram_download_failover/queue_stopping | 通过 |
| 原始/备用来源编辑撤销旧写，历史绑定不撤销后继 | execution barriers；source_account_downloads/telegram_download_failover | 通过 |
| 一账号故障不掩盖另一账号总体可用性 | AccountService.snapshot；telegram_account_runtime，四个直接/命令启用合同 | 通过 |

首轮/修复/最终实际结果及临时文件清理见 progress 的 R93。未运行整库测试、真实供应商或部署。总监听/下载持久预算与冷却在 P9；文件崩溃发布在 P8；其他客户端仓库未验证。

升级前停止服务并备份完整 SQLite/WAL/SHM、私有配置和关联文件；在隔离副本 initialize 后验证，失败恢复旧完整快照与配套源码。032–035 正式测试覆盖保留配置/墓碑/游标、迁移回滚和内部接替守卫，不提供删除迁移号降级。
