# R96 / P9 完成核对（2026-10-15）

源码 rc12 / schema 038；2710 collected only。多组定向回归按 classname/name 取最新结果，454 个不同用例通过，0 failure/error/skip；未运行整库，未访问真实供应商或升级真实数据库。

| 要求 | 实现与正式合同 | 结论 |
|---|---|---|
| 单后台与快速重启 | WorkerSession 在迁移前取得数据库 OS 文件锁，旧会话 ABANDONED；第二应用拒绝启动，启动取消释放锁；worker_sessions | 通过 |
| 长任务不被错误抢占 | session UUID 绑定 claim/执行/账号请求，15 秒心跳、120 秒派发租约；实际活动和清理中继续排他，心跳不增加编辑 revision | 通过 |
| 周期恢复及故障隔离 | 原 operations loop 每 15 秒轮转最多 100 条，逐项恢复下载、打包、发布/移动 journal；未知文件保留并报告，其他作品继续 | 通过 |
| 恢复有界且先确认远端停止 | recovery_count 最多自动恢复 3 次，再次中断失败；远端先 STOPPING/STOP_CONFIRMED 后失败，显式 retry/restart 清计数；worker_sessions/queue_budgets | 通过 |
| 本地/远端预算 | 下载和打包各 1；远端在途 1，未知推送/FAILED 残留/STOPPING 不假释放，完成后做种不占下载槽；queue_budgets/torrent_workflow/remote_stop_confirmation | 通过 |
| 账号与全局请求预算 | 普通/传输共池 4，监听另池 4；各账号各类别 1，先等账号再占全局，许可后重查停止/版本/session；account_request_limits/account_runtime | 通过 |
| TG 限流跨功能/重启有效 | Bot429/MTProto FloodWait 存 telegram_cooldowns，原激活意图到期恢复；停用不复活，health 脱敏；account_request_limits/account_service | 通过 |
| 停机/历史请求 | 最先阻止新领取与账号请求；等待实际请求/线程/进程树退出才释放会话。旧 EH 只读请求及账号网络回执标 UNKNOWN，不重放收费/登录 | 通过 |
| 037 升级与冻结 | 正式 worker_migration 使用旧 037 临时库升级；027–037 不改写，038 随本阶段提交冻结 | 通过 |

主要结果：first 111/0（53.918 秒），budget-first 126/0（46.024 秒），recovery 26/0（12.096 秒），accept 46/0（12.846 秒）；最后 final 155 条 3 fail（82.050 秒），两项旧 task DTO 断言错误包含可变 pack_intent，另一项 migration_count 仍期待 37。修正后 corrected 两完整文件 50/0。其他中间失败、挂起及修正见 progress，不能将重复组相加；全部已知测试工具会话终态。最终 454 以每节点最新结果汇总。

限制：Windows 实际硬退出及子进程树合同通过；Linux 新 guardian 仅静态检查，本机无可用 Linux 运行环境，实跑属于 P12 未完成项，不能据此声称跨平台验收。初始预算是保守保护值，没有真实 qBittorrent/性能调优数据。文件保障沿 P8 进程退出边界，不承诺掉电或磁盘损坏恢复。

升级/回退沿 R92：先停服务、完整 SQLite/私有配置/文件快照，隔离副本验证后升级；回退恢复完整旧快照及配套源码，不删除迁移记录。无新增环境变量或配置字段，.env.example 无需变更。

README/USAGE/API、PHASES/处理计划/task_plan/findings/progress/AGENTS 同步。立即精确暂存本阶段代码、正式测试与文档，本地单行提交并核验 SHA 后才进 P10。排除既有 models.py BOM 差异。TEMP/ehbot-p9-recovery-20261015-a 在提交核验和结果留痕后清理；不创建 .scratch。无推送、发布或部署；P10–P12 及总目标继续 active。

**P9 提交核实。** 本地提交 f60412b749ec1438d53decc240f628d6a0fc6ef7，37 个提交 blobs 与验收工作文件规范化内容逐项相同；暂存区为空，027–037 与上阶段一致，038 已冻结。提交前辅助检查误将新增 038 包含在旧迁移不变断言，已按 027–037 修正并完成提交后核验；没有据失败脚本声称预先清单验证通过。corrected 两文件 50/0/0/0（12.105 秒）。所有已知测试会话终态；TEMP 绝对路径及无重解析点已核对，共 2438 文件/676167314 字节，精确清理后剩余 0；无 .scratch。SHA/清理留痕随 P10 纳入，不另拆文档提交。
