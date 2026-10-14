# R91 / P4 完成核验（2026-10-14）

范围依据：已批准队列/多账号方案与实施计划 P4。源码 rc7、schema 030；仅本地阶段验收，没有真实库升级、供应商操作、推送、发布或部署。P3–P12 总目标继续 active，P5–P12 尚未交付。

| P4 要求 | 当前实现及证明 |
|---|---|
| 三类独立账号、不可变凭据版本、默认停用 | AccountRegistry、030；18 条 registry 合同含真实私有文件并发写、CAS、失败回滚、平台身份唯一、29→30 升级/重复初始化 |
| 旧单账号迁移及启停保留 | import_legacy 保留唯一导入标记，startup 只恢复明确 activation_requested；EH/Bot/TG_USER 三类停用→重启→启用复用同一保存值/session；删除后不复活 |
| 默认掩码、查看、编辑、明确清空 | HTTP reveal 的 CSRF/no-store/版本与 session 过滤、部分编辑/掩码拒绝；保存字段可显示/隐藏，真实 HTMX 保留节点测试证明不覆盖草稿 |
| 编辑/停用/删除先真实停止 | AccountLifecycle 的版本、回执与停止依赖同事务；复用 P3 QueueControl/DownloadService；账号 runtime 等完整 HTTP/线程退出，停止未知不切版本、不释放依赖 |
| 正确凭据版本覆盖所有实际入口 | wiring 使用 AccountService；Bot API 在监听借用内构造，TG user 按借用读取 API/session；下载、EH 元数据/归档、种子请求和手动添加验证/调用者写入共用账号上下文；数据库提交守卫拒绝迟到结果 |
| 共享命令 API、并发与回执 | Cookie/Bearer、CSRF、expected_revision、幂等冲突/重放、原子配置回执；网络先保存 PENDING，UNKNOWN 不重发；停止完成保存固定掩码 result，后续修改不改变原结果；WS 排除 reveal |
| 独立 TG 登录挑战与单次消费 | 多账号并行 send_code，取消 A 不动 B，保存 API/手机号保留；旧 challenge/版本拒绝，2FA 与错误验证码保留挑战；配置变化后的迟到登录结果不能保存 session；cancel 获锁后重核版本 |
| Web 与真实运行健康 | 列表 runtime、设置 account_runtime 分离持久启用意图与连接健康；旧无版本表单拒绝；创建/编辑响应丢失复用 key，捕获阶段不重复发；保留节点只绑定一次并读取新 CSRF；登录草稿/停止结果不被刷新覆盖 |
| 阶段运行限制 | P4 可保存多份配置，每类只启用一个；多 TG 监听/来源归属和 EH 轮询依赖 P5/P6/P7，不提前开启 |
| 文档、版本与交接 | README、USAGE、REALTIME_API、EHBot、AGENTS、PHASES、计划/task_plan/findings/progress 同步；030 冻结，027–029 不改写 |

验证：16 个 integration 文件完整运行 **321 passed / 0 failures / 0 errors / 0 skipped，274.521 秒**。最后登录取消版本校验后 service/accounts_api 两文件完整 **28/0，14.805 秒**；相同节点取后一次，共 **322 个不同用例最终通过**，不宣称最终源码单次跑完 16 文件或整库全绿。最新 **2280 collected，仅收集**，较 R90 净增 67。两个 Node DOM 脚本通过；compileall、JS syntax、CodeGraph sync、diff --check 通过。

16 文件：account_registry、account_lifecycle、account_runtime、account_service、accounts_api、connections_web、telegram_user_web、manual_add、database、downloads、exhentai_torrents、source_preflight、torrent_workflow、running_removal、realtime、settings_write_api。测试只接临时库/私有目录、HTTP/Telethon 替身；实际 MTProto 适配调用签名和下载产物通过真实应用 wiring 测试，未请求真实 Telegram。

真实供应商/代理/其他客户端/正式部署未验证；完整产物发布恢复、worker 心跳与预算分别属于 P8/P9。P5 接续统一 ingest_entries 键、待处理领取、补来源和历史冲突迁移，不能把 P4 的单运行限制当作最终多账号方案。
