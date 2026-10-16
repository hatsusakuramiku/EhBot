# 下载队列、多账号与共享 API 处理计划

日期：2026-10-13。依据：[已批准方案](DOWNLOAD_QUEUE_AND_SHARED_API_PROPOSAL.md)、[项目审阅](PROJECT_REVIEW_2026-10-13.md)。授权原文：「方案可行，着手规划处理计划，之后再按处理计划进行处理（注意留痕，一遍其他agent快速了解进度）」。已授权按计划实施，不重复请求开工许可；发布、推送和部署未授权。

## 快速接手（每次结束工作前更新）
| 项目 | 当前事实 |
|---|---|
| Current phase | P11 / R98 complete; version 0.3.0rc14; committed locally as the final P11 local commit SHA verified in Git history |
| Next action | Begin P12 after final SHA/status verification; do not add another P11 commit |
| 已完成 | P0–P11; P10=f03c977; P11=the final P11 local commit SHA verified in Git history |
| 工作区 | P11 files committed; pre-existing app/downloads/models.py BOM delta remains unstaged |
| 版本/schema | 0.3.0rc14 / schema039; 027–039 frozen; no new migration |
| Verification baseline | P11 focused regression: 131 passed, 0 failure/error/skip; compileall, CodeGraph sync, and diff-check passed |
| 边界 | No real data/private, supplier, database upgrade, deployment, push, or release |

027–038 已冻结且本阶段未修改；039随P10提交冻结，旧实验039不能靠重复initialize自动补表，不手改schema_migrations、不运行真实库升级。P8/P9 TEMP已清理；P10 TEMP为ehbot-p10-api-20261015-a，提交核验/结果留痕后精确清理；.scratch不存在。

接手先读 PHASES，再读本表、当前阶段和 progress 最新 R 条目。task_plan 是清单，本文件是完整执行边界，findings 是调查证据，progress 是实际验证日志。先核对 git status，不能把进行中工作重新从空白实现。

## 阶段顺序与验收门槛

每阶段可运行、可回滚、可验收，一阶段一个本地提交。后续 R/迁移号实施时分配；不能用计划号冒充已完成 R 阶段。

阶段提交门槛（2026-10-15 用户明确要求）：每阶段验收通过后，立即暂存并提交该阶段的代码、正式测试和同步文档，检查暂存差异并核实提交 SHA，再进入下一阶段。仅暂存不算交付，不跨阶段积攒提交，不混入未完成阶段的改动；提交失败须记录原因并标为「待提交」。本地阶段提交已获授权，无需重复询问；推送、发布和部署另行授权。

| ID | 状态 | 处理范围 | 验收 / 依赖 |
|---|---|---|---|
| P0 | complete | 批准、处理计划与接手入口 | 方案/计划互链、既有证据保留 |
| P1 / R88 | complete | 固定管理员身份、联合登录、可改名、多端会话、原子令牌对/刷新、配置并发 API、Web/WS | 12 个新合同及相关 370 个不同用例最终通过；2113 collect；rc4/026；提交见工作区栏 |
| P2 / R89 | complete | task/attempt、revision/generation、回执/operation、审批事实、旧 ID 映射、任务命令入口收口 | 一个下载槽，事务意图/回执一致；既有 worker 原子认领，无双调度；2026-10-14 逐项复核，28 个合同与相关 19 文件 460 条完整通过。实际停止接 P3 |
| P3 / R90 | complete | 真实取消/切源/移除、本地子任务/线程/进程树收束、远端查回与停止确认、旧 poller 屏障、最小 staging | 029/rc6；目标预检、refile 屏障、持久移除/清理与重加等待、共享资源拒绝及归属核实；六项原触发转正式回归；628 个不同用例最终通过，见 R90 audit |
| P4 / R91 | complete | 账号注册/独立配置版本、掩码查看/部分编辑/启停/删除、共享 API/UI | 实际 startup/provider/API/UI 接通；322 个不同用例最终通过及两个 DOM 脚本通过，rc7/030。每类型只启用一个；停用保留凭据，编辑等待旧执行停止，固定停止结果保留版本。见 R91 audit |
| P5 / R92 | complete | ingest_entries 统一身份、历史回填与单账号摄取/发布 | 2ea49f2，rc8/031；447 个不同用例最新通过。MTProto 可靠收件箱/来源专属迁移及下载接替按原计划 P6 |
| P6 / R93 | complete | 多 TG 独立监听/游标/来源 CAS 与有限串行下载接替 | rc9/035，419 distinct passed + DOM root/proxy；本地提交 c61af7e，58 blobs 已核对；详 R93 audit |
| P7 / R94 | complete | EH 公平有限池/能力退避/收费未知核实/认证种子 | 31b5b00，rc10/036，363 distinct passed；详 R94 audit |
| P8 / R95 | complete | artifact 生产者/精确打包与 refiling 依赖、发布/移动恢复、受管取消、流水线意图 | 89ef4b9，rc11/037；590 distinct passed / 2687 collect-only；37 blobs 已核实，见 R95 audit |
| P9 / R96 | complete | 会话/心跳、周期恢复/隔离/轮转、有界中断恢复、独立预算与 TG 持久冷却 | f60412b，rc12/038；454 distinct passed / 2710 collect-only；Linux 实跑留 P12 |
| P11 | complete | R98: approval rotation, thumbnail stale-PENDING protection, lock-free ComicInfo preparation, fair metadata synchronization | 131 focused tests passed |
| P12 | pending | Final HTTP/WS/CBZ acceptance, migration/recovery verification, Linux guardian execution | Pending |

默认串行实施，不因“其他 agent 接手”自行启动 agent。多账号工作区允许并行启用，须在 P5/P6 完整验收前补齐去重、来源管理和在途停止边界，不将当前中间态发布。P3 已含最小发布屏障，不能等 P8 才防旧写。每个切片自带必要并发保护，P10 完成统一核对而非首次加保护。

## P1 具体步骤

1. **迁移与身份**：026 增管理员 revision/认证代次，复用稳定 id；按唯一身份启动，默认 admin 仅新安装创建。记录旧 Cookie 过渡；异常多管理员不静默取第一行。验证旧库、新库、改名重启。
2. **共享认证与配置**：Web/API 共用账户名＋密码和失败计数；profile GET/PATCH、改密码 JSON API 支持当前密码/expected_revision。验证错名/密码/缺字段、联合节流、初始限制、并发修改与旧验证结果。
3. **凭据事务**：令牌对签发、refresh 消费及后继同事务；校验认证代次，改密码原子撤销旧密码派生凭据，API Key 保留。验证竞争、回滚、独立族及改密码/签发交错，秘密只存哈希。
4. **Web/WS**：联合登录页、管理员编辑与冲突输入保留、动态 whoami、会话有效性核对与失效 WS。仅改名保留会话。验证独立登录/注销、旧 WS、部署前缀。
5. **验证与交接**：按实际改动选择 authentication/api_auth/realtime/settings_web/settings_write_api/database/credentials 及新增合同用例；老登录辅助函数显式补 username，不放宽产品迁就旧测试。同步本阶段实际行为到 README/USAGE/REALTIME_API/EHBot/AGENTS，记录首次失败、修复与最终计数，再完成阶段。

P1 升级/回退边界：026 随启动事务执行，新库与旧 025 库均测试；发现多个管理员时整笔迁移回滚，不静默删账号。升级使无设备记录的旧 Cookie 失效，旧密码 API 客户端必须补 username。运维升级前停服务并备份整个 SQLite 数据目录；若需退回旧代码，停服务后恢复升级前快照及旧代码，不能只删 schema_migrations 记录或只降代码（旧代码硬编码 admin，与改名后的库不兼容）。本阶段不对真实数据库执行升级/回退，快照之后的数据需单独评估恢复。

定位：app/db/database.py admin/credential，002/023 迁移，app/web/routes/auth.py，app/api/auth.py/deps.py，app/web/deps.py，realtime/WS，app/wiring.py，password/settings 模板及快照。使用 CodeGraph 当前源码定位，不凭旧行号盲改。

## 执行与留痕规则

### P3 实施步骤（已验收，2026-10-14）

1. 子请求失败/超时/取消均收束；线程 await 被取消后仍等待真实线程退出。用受控 HTTP 请求和真实阻塞线程验证，不以协程取消冒充物理停止。
2. 真实领取身份绑定 task/attempt/generation，传播到供应商和数据库提交；在写事务内检查身份，陈旧产物、元数据与自动打包一并回滚。按 attempt 隔离暂存路径，旧执行不覆盖后继文件。
3. 独立处理持久 queue_operations：取消受管子任务并 await，远端根据已持久推送意图查回资源、确认停止；网络失败保留 STOPPING 和槽，不能吞异常释放槽。
4. 停止确认与新来源入队在同一短事务完成；运行移除持久登记清理，停止、文件处理和业务删除可恢复，操作记录保留。
5. 六个原始触发条件转正式回归，完成全阶段定向验证、迁移、文档、R90 接手记录后才验收 P3。P4–P12 范围保持不变。

假设与边界：项目仍为单后台进程、单管理员，多端共用数据库裁决；远端停止不能由本地取消推断。P3 已防陈旧提交和重叠执行；P9 完善 worker_session/心跳和跨重启恢复预算，P8 完善文件发布崩溃恢复。027–029 已交付迁移冻结，不改写。

### P4 实施步骤（进行中，P3 已验收）

1. 新增独立账号注册与凭据版本存储，数据库只保存 UUID、配置版本、私有文件引用、非敏感身份和启停状态；每个待提交版本使用独立文件名，避免两个客户端同版本编辑先覆盖文件再竞争数据库。用真实并发编辑、文件写失败及事务回滚验证一个赢家、无悬空引用和秘密不入库。
2. 迁移旧单账号配置至注册表，保留旧读取入口作为薄适配；停用保留凭据与 session，恢复使用已保存配置。先维持兼容单账号 runtime；全部独立监听属于 P6，EH 轮询属于 P7。
3. 账号编辑、启停、删除与队列执行绑定：凭据变更或停用先撤销并等待旧版本的受管执行/监听停止，未确认停止保留待处理操作；纯别名变化不重登。相同平台身份启用冲突由唯一约束裁决，测试连接失败不清空填写内容。
4. 共享 JSON API 提供列表、创建、部分编辑、显式查看、启停、连接测试、登录挑战/取消和删除；配置 revision、命令回执及 Cookie/Bearer/CSRF 同时接入。查看只返回允许查看的配置字段，不导出 Telethon session；响应禁止缓存，错误和 WS 不含秘密。
5. Web 复用这些 API，已保存字段默认显示固定掩码，可显示、隐藏、修改；未触碰字段不发送，清空采用显式动作，登录不重填已保存 API 信息。完成 API/Web/迁移/停止竞态定向验证、文档与 R91 接手记录，再提交 P4。

### P2 / R89 实施裁决与接续边界

- 采用 `app/downloads/queue.py` 的短事务命令核心与 027 SQL 兼容 adapter。`download_jobs` 仍是现有 provider/页面投影；稳定 `task_id` 和独立 `queue_tasks/queue_attempts` 保存任务与真实执行身份，不再将旧整数行当作可复用的任务身份。历史领取次数不伪造成逐条尝试。
- SQL trigger 覆盖现有 conversion、delegated EH/Telegraph、torrent poller 和移除写者；作品下载槽有唯一索引，运行记录删除有最终数据库守卫。现有 download/conversion worker 用 `BEGIN IMMEDIATE` 原子认领并读取 task/attempt/generation；没有第二套调度器。
- `/jobs` 的单项与批量、普通表单、WS 转发统一要求已读取的 UUID/revision/幂等键。回执查询先于当前版本核对，固定管理员跨凭据共享作用域；受理、操作及回执一起提交。`GET /tasks/{task_id}` 可读旧任务/尝试和独立收尾操作。七天回执清理保留未结束操作。
- 审批独立持久化，迁移只读取 APPROVED 状态或 APPROVE/AUTO_APPROVE 审计；任意历史 job 不证明审批。历史多活动来源使 027 整笔回滚，必须先用旧版本核实/停止冲突任务，不静默杀远端或假定其已停。
- P2 的运行取消/切源/远端重试/停止做种只持久化意图；STOPPING 保留作品槽并拒绝旧完成/产物提交。实际停止确认、操作恢复、attempt 全路径屏障与按尝试 staging 在 P3；旧候选/文件的完整清理以及全域客户端草稿 CAS 按 P3/P10 接续。P2 不可作为已完成物理取消的可部署版本。
- P8 接续精确产物版本及打包依赖，P9 接续完整心跳/恢复预算。当前 DTO 继续兼容旧状态词与 integer job_id，新增 UUID/revision/generation/attempt_id；状态/API 最终统一在 P10 核对。
- 2026-10-14 完成复核补漏：028 冻结已结束 attempt，后续 STOPPING/资源核实不覆盖原错误；内部重试的唯一槽竞争返回相同业务错误；打包与下载识别同一 SQL 陈旧提交拒绝。原 027 不改写，源码仍为未发布 rc5。完整要求映射、复现及当前证据见 R89_COMPLETION_AUDIT。

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
