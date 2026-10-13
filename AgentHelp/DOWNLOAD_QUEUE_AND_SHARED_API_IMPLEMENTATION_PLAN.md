# 下载队列、多账号与共享 API 处理计划

日期：2026-10-13。依据：[已批准方案](DOWNLOAD_QUEUE_AND_SHARED_API_PROPOSAL.md)、[项目审阅](PROJECT_REVIEW_2026-10-13.md)。授权原文：「方案可行，着手规划处理计划，之后再按处理计划进行处理（注意留痕，一遍其他agent快速了解进度）」。已授权按计划实施，不重复请求开工许可；发布、推送和部署未授权。

## 快速接手（每次结束工作前更新）

| 项目 | 当前事实 |
|---|---|
| 当前阶段 | P2 / R89 核心验收完成；下一阶段 P3（物理停止未实施） |
| 已完成 | P0–P2；认证、多端会话、task/attempt、独立审批、作品槽、任务命令回执/操作与 HTTP/Web/WS 接通 |
| 下一动作 | P3 从 queue_operations 开始：操作执行/恢复，本地子任务停止并 await、远端停止查回，attempt/generation 全提交路径与暂存屏障；A 未停 B 不开。不要重做 P1/P2 |
| 工作区 | R88=cfa649d；R89 本地提交标题 feat(queue): persist task identities and atomic command receipts，最终 SHA 以 git log 为准；.scratch 原复现与 R89 XML/collect 结果保留未提交 |
| 版本/schema | 源码 0.3.0rc5、027 / 27；仅临时库迁移验证，无真实库升级/发布 |
| 验证基线 | 2134 collected（+21，仅收集）；R89 相关 19 文件、453 个不同用例最终通过（部分节点定向补测）；首次失败与修正见 progress/R89；不是整库全绿 |
| 环境 | Windows PowerShell；本 checkout 无 .venv，使用 E:/Workshop/VSCode/EhBot/.venv/Scripts/python.exe，PYTHONPATH 指向本 checkout |
| 边界 | 不读真实 data/private；临时数据库/文件测试；真实供应商、其他客户端仓库与部署另记未验证 |
| 现场问题 | rg 不可用，用 CodeGraph/Select-String；git ignore 权限告警不影响查询，.git 写入需工具审批 |

接手先读 PHASES，再读本表、当前阶段和 progress 最新 R 条目。task_plan 是清单，本文件是完整执行边界，findings 是调查证据，progress 是实际验证日志。先核对 git status，不能把进行中工作重新从空白实现。

## 阶段顺序与验收门槛

每阶段可运行、可回滚、可验收，一阶段一个本地提交。后续 R/迁移号实施时分配；不能用计划号冒充已完成 R 阶段。

| ID | 状态 | 处理范围 | 验收 / 依赖 |
|---|---|---|---|
| P0 | complete | 批准、处理计划与接手入口 | 方案/计划互链、既有证据保留 |
| P1 / R88 | complete | 固定管理员身份、联合登录、可改名、多端会话、原子令牌对/刷新、配置并发 API、Web/WS | 12 个新合同及相关 370 个不同用例最终通过；2113 collect；rc4/026；提交见工作区栏 |
| P2 / R89 | complete | task/attempt、revision/generation、回执/operation、审批事实、旧 ID 映射、任务命令入口收口 | 一个下载槽，事务意图/回执一致；既有 worker 原子认领，无双调度；21 新合同与相关 453 个用例最终通过。实际停止接 P3 |
| P3 | pending | 真实取消/切源/移除、本地子任务收束、远端推送查回与停止确认、旧 poller 屏障、最小 staging | 依赖 P2；六个专项复现转回归；A 未停 B 不开，移除不复活 |
| P4 | pending | 账号注册/独立配置版本、掩码查看/部分编辑/启停/删除、共享 API/UI | 依赖 P2–P3；先兼容单账号 runtime；断开不删凭据，编辑可停止旧执行 |
| P5 | pending | ingest_entries 统一键附加表、各入口归一化、待入库领取、补来源、转发身份与历史迁移 | 依赖 P2；一键一候选/处理者，多观察保留；先裁决历史冲突再加唯一约束 |
| P6 | pending | 多 Bot/TG 用户独立监听/游标/session/挑战/缓存、observation 选路及来源账号优先 | 依赖 P3–P5；同频道/转发/同画廊并发验收，file_id 不串 Bot，接替无重叠 |
| P7 | pending | EH 公平轮询、有界失败集合、能力/冷却、未知费用核实 | 依赖 P3–P4；全账号失败终止；旧请求结束才换账号，未知费用不重复申请 |
| P8 | pending | artifact 生产者/精确打包依赖、可恢复文件发布、受管打包取消、流水线意图 | 依赖 P2–P3；保旧有效文件，只打包指定新产物，无线程/子进程晚发布 |
| P9 | pending | worker_session/心跳、周期 reconcile、停止/清理恢复、账号/全局/远端预算 | 依赖 P6–P8；快速重启恢复，长任务不误回收，STOPPING 不假释放槽，费用不重复 |
| P10 | pending | 全域 revision/幂等/批量结果、v2 与旧映射、Web 共享 JSON、WS/草稿冲突 | 前置切片随交付自带保护；此阶段逐域收口，无无条件写旁路，不自动覆盖草稿 |
| P11 | pending | 审阅其余缺陷：审批 LIMIT 饥饿、ComicInfo 写锁、缩略图 PENDING 竞态 | 与前述机制共用修复；复现原触发条件，不另造平行机制 |
| P12 | pending | 合同验收、真实本地 HTTP/WS/CBZ、迁移/恢复断点、最终交接 | 定向回归与 collect 检查；性能须实测，其他客户端/真实供应商/发布部署分开记 |

默认串行实施，不因“其他 agent 接手”自行启动 agent。多账号启用前必须完成去重、来源身份和可停止执行。P3 已含最小发布屏障，不能等 P8 才防旧写。每个切片自带必要并发保护，P10 完成统一核对而非首次加保护。

## P1 具体步骤

1. **迁移与身份**：026 增管理员 revision/认证代次，复用稳定 id；按唯一身份启动，默认 admin 仅新安装创建。记录旧 Cookie 过渡；异常多管理员不静默取第一行。验证旧库、新库、改名重启。
2. **共享认证与配置**：Web/API 共用账户名＋密码和失败计数；profile GET/PATCH、改密码 JSON API 支持当前密码/expected_revision。验证错名/密码/缺字段、联合节流、初始限制、并发修改与旧验证结果。
3. **凭据事务**：令牌对签发、refresh 消费及后继同事务；校验认证代次，改密码原子撤销旧密码派生凭据，API Key 保留。验证竞争、回滚、独立族及改密码/签发交错，秘密只存哈希。
4. **Web/WS**：联合登录页、管理员编辑与冲突输入保留、动态 whoami、会话有效性核对与失效 WS。仅改名保留会话。验证独立登录/注销、旧 WS、部署前缀。
5. **验证与交接**：按实际改动选择 authentication/api_auth/realtime/settings_web/settings_write_api/database/credentials 及新增合同用例；老登录辅助函数显式补 username，不放宽产品迁就旧测试。同步本阶段实际行为到 README/USAGE/REALTIME_API/EHBot/AGENTS，记录首次失败、修复与最终计数，再完成阶段。

P1 升级/回退边界：026 随启动事务执行，新库与旧 025 库均测试；发现多个管理员时整笔迁移回滚，不静默删账号。升级使无设备记录的旧 Cookie 失效，旧密码 API 客户端必须补 username。运维升级前停服务并备份整个 SQLite 数据目录；若需退回旧代码，停服务后恢复升级前快照及旧代码，不能只删 schema_migrations 记录或只降代码（旧代码硬编码 admin，与改名后的库不兼容）。本阶段不对真实数据库执行升级/回退，快照之后的数据需单独评估恢复。

定位：app/db/database.py admin/credential，002/023 迁移，app/web/routes/auth.py，app/api/auth.py/deps.py，app/web/deps.py，realtime/WS，app/wiring.py，password/settings 模板及快照。使用 CodeGraph 当前源码定位，不凭旧行号盲改。

## 执行与留痕规则

### P2 / R89 实施裁决与接续边界

- 采用 `app/downloads/queue.py` 的短事务命令核心与 027 SQL 兼容 adapter。`download_jobs` 仍是现有 provider/页面投影；稳定 `task_id` 和独立 `queue_tasks/queue_attempts` 保存任务与真实执行身份，不再将旧整数行当作可复用的任务身份。历史领取次数不伪造成逐条尝试。
- SQL trigger 覆盖现有 conversion、delegated EH/Telegraph、torrent poller 和移除写者；作品下载槽有唯一索引，运行记录删除有最终数据库守卫。现有 download/conversion worker 用 `BEGIN IMMEDIATE` 原子认领并读取 task/attempt/generation；没有第二套调度器。
- `/jobs` 的单项与批量、普通表单、WS 转发统一要求已读取的 UUID/revision/幂等键。回执查询先于当前版本核对，固定管理员跨凭据共享作用域；受理、操作及回执一起提交。`GET /tasks/{task_id}` 可读旧任务/尝试和独立收尾操作。七天回执清理保留未结束操作。
- 审批独立持久化，迁移只读取 APPROVED 状态或 APPROVE/AUTO_APPROVE 审计；任意历史 job 不证明审批。历史多活动来源使 027 整笔回滚，必须先用旧版本核实/停止冲突任务，不静默杀远端或假定其已停。
- P2 的运行取消/切源/远端重试/停止做种只持久化意图；STOPPING 保留作品槽并拒绝旧完成/产物提交。实际停止确认、操作恢复、attempt 全路径屏障与按尝试 staging 在 P3；旧候选/文件的完整清理以及全域客户端草稿 CAS 按 P3/P10 接续。P2 不可作为已完成物理取消的可部署版本。
- P8 接续精确产物版本及打包依赖，P9 接续完整心跳/恢复预算。当前 DTO 继续兼容旧状态词与 integer job_id，新增 UUID/revision/generation/attempt_id；状态/API 最终统一在 P10 核对。

- 用户已授权方案内实施；每阶段从调查、实现到测试/文档/提交闭环，不重复问许可。超出方案的业务变化再对齐。
- 每阶段一个应用实现，旧入口薄适配；迁数据却保留全部旧写者绕过新核心的中间态不交付。
- 定向测试，collect-only 与实际通过分开；完整测试/发布遵守仓库门槛。所有测试只接临时库和受控外部替身。
- 不热迁移真实数据、不双发副作用；写升级/回滚入口，测试库验证。版本提升不代表发布。
- 提交前 diff --check、必要语法检查和定向验证；审批阻止提交则记录已验证未提交和原因，保留工作区，不声称提交。
- PHASES 更新一屏状态，task_plan 更新清单，progress 记录 R 的实际文件、schema/版本、命令/结果、失败/未执行项、SHA/未提交原因；本表写下一步精确入口。
- 上下文压缩或换 agent 沿同一计划继续，不重建计划、不重复迁移。未完成时明确标进行中，不能把计划、源码实现和外部验收混记为完成。

## 执行问题记录

| 日期 | 问题 | 处理 |
|---|---|---|
| 2026-10-13 | 首次计划补丁的 findings 上下文不匹配，整批未写入 | 核对实际末行后重做精确补丁，无部分写入/产品改变 |
| 2026-10-13 | 读取模板误用 app/templates；WebSocket 误查 app/realtime；同一文件 delete/add 补丁被拒 | 实际路径为 app/web/templates 与 app/api/realtime.py；分开补丁后恢复文件 |
| 2026-10-13 | 旧测试登录契约批改遇 UTF-8 BOM；插入字段产生行末空白 | 按 utf-8-sig 读取，清理本次插入格式；不放宽产品登录与 revision 要求 |
| 2026-10-13 | P1 首轮 79 条中 2 条失败：whoami 新 revision、迁移 26 的旧断言 | 更新契约断言；初次结果保存在 .scratch/r88-auth-first.xml，待复验 |
| 2026-10-13 | 新合同 7 条中 2 条失败：错误 WS 命令名及刷新事件抢在认证错误前 | 使用实际命令目录并按帧类型读取；多端测试只启动一次 lifespan，API Key 测试修正路由并断言确已生成 |
| 2026-10-13 | 核心回归 126 条中 3 条旧契约失败：要求打印引导密码、读取全局 WS 代次 | 按禁止输出秘密及设备独立撤销的新契约更新；3 条定向复验通过 |
| 2026-10-13 | Compose 文件名猜测错误、JUnit 按首行解析失败 | 根目录实际为 compose.yaml/compose.deploy.yaml；按完整 XML 解析，只输出计数 |
| 2026-10-13 | pytest atexit 清理旧 pytest-current 链接出现 WinError 5 | 测试退出码/XML 判断独立；后续指定 workspace basetemp 避免碰旧临时目录 |
| 2026-10-13 | 设置/API 回归 223 条中 3 条 SSE 夹具失败：伪造 authenticated 不再通过认证 | 临时库创建真实设备会话后直调流，6 个 SSE 用例复验通过；不削弱产品认证 |
| 2026-10-13 | smoke 选择漏 class 前缀造成收集失败 | 补全节点名；14 条通过，另补 2 条间接登录夹具通过；移除本轮临时修改/选择脚本，原复现保留 |
| 2026-10-13 | 首次本地 commit 失败：当前 checkout 缺 Git author 配置 | 最近三次提交作者/提交者一致；后续命令临时复用该身份，不改全局配置，不推送 |
| 2026-10-13 | P2 调查 CLI 不支持 symbols、误猜 torrent/API/test 文件路径，首次 patch 重复指向 queue.py 被拒 | 使用 query/explore 与目录清单确认路径；重复目标合为一次补丁，没有部分写入 |
| 2026-10-13 | R89 首轮下载 23 条有 2 个 FIFO 夹具失败；adapter 204 条有 33 个失败 | 同作品多活动任务夹具改为不同作品；种子旧立即停止断言改为持久受理/确认边界；schema 26→27；原 XML 保留 |
| 2026-10-13 | 新 HTTP/WS 合同首次跨事件循环停止 worker、WS 参数形状错误；后续回归缺审批证据与旧 API allowed 丢失 | portal.call 在 app loop 停止测试 worker；按 params/body 协议；夹具显式审批，恢复 allowed 返回；不是放宽产品契约 |
| 2026-10-13 | 源按钮旧测试期望 TG 与 Telegraph 同时活动；重下提前计数、工作详情旧表单缺前置条件 | 竞争来源明确拒绝；重下走核心且真实领取才计数；表单补观测身份/版本/幂等键；相关失败均定向复验通过 |
