# R94 / P7 完成核对（2026-10-15）

rc10/036；2661 collected only；24 文件 362/0，最终四文件 42/0，按节点最新 363 个不同用例通过。无整库测试、真实供应商、发布或部署。

| 要求 | 实现 / 正式合同 | 结论 |
|---|---|---|
| 公平原子起点，状态读取不推进 | AccountService selector/036；eh_account_rotation | 通过 |
| 匿名 gdata 不借账号，批量不重复公共请求 | GdataClient/ExHentaiService；gdata/eh_metadata_accounts | 通过 |
| 有限元数据池、精确版本、真实退出后接替、成功租约 | MetadataAccountPool；eh_metadata_accounts/account_service/manual_ingest_identity | 通过 |
| 凭据/能力健康与 SITE/GDATA 共享 Retry-After，重验恢复 | ExHentaiHealth/AccountRegistry；eh_health/eh_errors/exhentai_api | 通过 |
| 归档/种子使用同队列独立 attempt、有限池跨重启 | DownloadAccountPool/QueueControl；eh_download_accounts/queue_control | 通过 |
| GP/认证明确失败后才换账号，未知费用不换账号重复提交 | ArchiveRequests；eh_archive_intents/eh_download_accounts | 通过 |
| 原账号只读核实、READY 下载重试复用收费定位符 | 私有 SecretStore/intent 引用；archive_intents/download_accounts | 通过 |
| 取消/停用等待真实清理，结果未知仍保留 | AccountLifecycle/runtime；download_accounts/telegram_download_failover/provider_late_results | 通过 |
| .torrent Cookie 隔离、infohash 校验后推送，磁链无需 EH | TorrentFileFetcher/TorrentService；torrent/torrent_workflow/download_accounts | 通过 |
| 升级不改冻结迁移，当前 HTTP/Web/WS 继续同 API | 036/既有共享入口；database/account_service/queue_control/source_preflight | 通过 |

边界：未知费用无法从原账号确认时保持失败并阻止新收费；不承诺费用撤销，也不将普通表单误当未扣费证明。P8 接精确产物和可恢复文件发布；P9 接 worker session/持续心跳/全局与账号预算；P10 接全域协议与结构化结果核对。其他客户端仓库未测试。

详细首次失败、修复、最终结果和临时目录清理见 progress 的 R94。升级/回退沿 R92 完整快照步骤；只在新临时库验证，未升级真实数据库。

本地阶段提交 `31b5b0077709c1526f2f52da9471e99f2b8f9211`；40 个 blobs 与提交前 SHA256 逐项一致，暂存区为空，027–036 与提交相同。阶段提交后才接 P8。SHA 核验留痕随下一阶段纳入。
