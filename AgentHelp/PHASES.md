# 阶段总览（先读这里，再按需定位 `progress.md`）

`progress.md` 是逐阶段的详细实现与排障记录（5000+ 行）。本文是它的**入口索引**：
每条阶段一句话说清「做了什么、为什么」，需要细节时按“定位”列的行号跳到 `progress.md`。

维护约定：每完成一个阶段，**先在这里补一行，再去 `progress.md` 追加完整 R 条目**；
两处的 R 编号、版本与测试基线必须一致（`AGENTS.md` 的基线链是最终裁决）。

## 一、v1 实现阶段（2026-08-19 ～ 2026-08-22，重构前）

| 阶段 | 一句话 | 定位 |
|------|--------|------|
| 需求与计划 | 收敛 Telegram / ExHentai / CBZ / Docker 需求，产出 `EHBot.md`、`DEVELOPMENT_PLAN.md` | `progress.md:3` |
| 基础与持久化 | Python 3.12 + FastAPI + SQLite WAL、引导管理员、CSRF、登录锁定、健康检查、Docker | `progress.md:59` |
| 外部连接 | Token-only Bot API 轮询、ExHentai Cookie 校验、凭据私有文件、连接管理页 | `progress.md:76` |
| 候选摄取与审核队列 | Update 归一化、媒体组/回复合并、ACCEPT/IGNORE、候选队列与详情页、来源白名单 | `progress.md:101` |
| 审核动作与元数据编辑 | 通过/驳回/待补充、字段手改与来源标注 | `progress.md:125` |
| Telegram 媒体下载 | Bot API 下载、持久化任务、worker 认领与租约 | `progress.md:172` |
| ZIP→CBZ 与 ComicInfo | 流式转换、ComicInfo.xml 映射 | `progress.md:225` |
| ExHentai 元数据与归档 | gdata 元数据、HTML 回退、汉化标签、原档下载 | `progress.md:265` |
| 元数据规则过滤 | 按标签/语言/类别/评分在来源层过滤 | `progress.md:301` |
| 下载来源链四步 | Telegram → EH 种子 → telegra.ph 预览页；每步一个提交 | `progress.md:846` |
| 归档处理与 7-Zip | 可扩展归档后端、安全门禁、工具链托管 | `progress.md:584` |
| 运营者缺陷修复 | 历史页、自动打包、正则自动审批、手工加任务等 | `progress.md:1159` |

## 二、v2 重构（R0 起，`progress.md:1225` 之后）

R7 已随范围收窄删除；编号不重用，故从 R6 直接到 R8。

| R | 版本 / 日期 | 一句话 | 基线 | 定位 |
|---|-------------|--------|------|------|
| R0 | 2026-08-25 | 重构基线校准：真实基线 439（旧文档写 427），脚手架与规范 | 439→481 | `progress.md:1227` |
| R1 | 2026-08-25 | JSON API 层 + 共享审核编排；`main.py` 拆分起步 | 481→524 | `progress.md:1280` |
| R2 | 2026-08-25 | 封面缩略图服务；顺带修 `artifacts.size_bytes` 存成页数的旧缺陷 | 524→569 | `progress.md:1329` |
| R3 | 2026-08-26 | 设计系统与共享组件（`ui.css` / `components/ui.html` / `/ui-kit`） | 569→592 | `progress.md:1398` |
| R4 | 2026-08-26 | 活动域：队列 / 打包 / 历史三 Tab | 592→635 | `progress.md:1479` |
| R5 | 2026-08-26 | 候选域与审核流：六 Tab、封面网格、批量条、字段抽屉 | 635→663 | `progress.md:1566` |
| R6 | 2026-08-26 | 统一作品详情 `/works/{id}` 与生命周期时间线 | 663→708 | `progress.md:1641` |
| R8 | 2026-08-27 | 设置域收敛：八个 Tab 聚合一页 | 708→809 | `progress.md:1712` |
| R9 | 2026-08-27 | 收尾切换：旧页/旧模板清理、部署缺口（`THUMBNAILS_ENABLED`、`APP_SECRET_KEY`） | 809→820 | `progress.md:1796` |
| R10 | 2026-08-28 | 已下载域（§1.3.1）：清单 + 批量打包/移除/重下 + 单件改名 | 820→866 | `progress.md` 见 R10 |
| R11 | 2026-08-29 | 归档路径可控：详情页钉路径、批量按模板重算、路径钉表 | — | `progress.md:2081` |
| R12 | v0.2.3 · 2026-08-29 | 日志管线：脱敏、JSON 格式、保留策略 | — | `progress.md:2260` |
| R13 | v0.2.4 · 2026-08-29 | 打包失败可读日志 + 三级日志设置 | — | `progress.md:2371` |
| R14 | v0.2.5 · 2026-09-03 | 复审：日志字段白名单、安全头、连接生命周期 | — | `progress.md:2471` |
| R15 | v0.2.6 · 2026-09-04 | 四个运营者缺陷 + 无障碍审查整改 | — | `progress.md:2615` |
| R16 | v0.2.7 · 2026-09-05 | 三个“出不去”的状态 + 一个说谎的表单 | — | `progress.md:2807` |
| R17 | v0.2.8 · 2026-09-05 | 运行日志页 `/logs`（SSE、级别下限、缓冲区+文件） | — | `progress.md:2969` |
| R18 | v0.2.9 · 2026-09-05 | `box-sizing` 缺失导致输入框溢出，连带网格/换行修复 | — | `progress.md:3139` |
| R19 | v0.2.10 · 2026-09-06 | 六个运营者缺陷（就绪看产物、先取元数据、条件行数等） | — | `progress.md:3241` |
| R20 | v0.2.11 · 2026-09-07 | 全站 HTMX 局部刷新与两个顺序缺陷 | — | `progress.md:3419` |
| R21 | 2026-09-08 | WebUI 日志等级 + UTC 日志分流 | — | `progress.md:3539` |
| R22 | v0.2.12 · 2026-09-09 | Windows 便携式托管 7-Zip | — | `progress.md:3547` |
| R23 | v0.2.13 · 2026-09-12/16 | 详情页导航与人工补料；审批一次性门 | — | `progress.md:3555` |
| R24 | 2026-09-19 | 自动审批 DSL 重写为纯类 SQL + 逐条大小写开关 | — | `progress.md:3576` |
| R25 | 2026-09-19 | 归档路径规则：按条件匹配路径模板 | — | `progress.md:3598` |
| R26 | 2026-09-21 | 换页后确认弹窗不关闭 | — | `progress.md:3612` |
| R27 | 2026-09-25 | 归档路径预填 + 一键重新归档 | — | `progress.md:3631` |
| R28 | v0.2.17 · 2026-09-26 | AI 供应商：多供应商、多 Key 轮询、主力+备用链 | — | `progress.md:3739` |
| R29 | v0.2.18 · 2026-09-26 | AI 路径决策接入：来源、prompt、指纹缓存、回退 | — | `progress.md:3817` |
| R30 | v0.2.19 · 2026-09-26 | AI 整库重排：只移动不重打包、强制、试跑、并发/流式 | — | `progress.md:3921` |
| R31 | v0.3.0rc1 · 2026-09-26 | AI 供应商按 AstrBot 重写：两层管理 + 全局链 + 页面覆盖 | — | `progress.md:4007` |
| R32 | v0.3.0rc1 · 2026-09-26 | 识别藏在 HTTP 200 里的供应商错误（MiniMax） | — | `progress.md:4081` |
| R33 | v0.3.0rc1 · 2026-09-26 | 被拒 API 请求一条日志定位 | — | `progress.md:4121` |
| R34 | v0.3.0rc1 · 2026-09-26 | 已下载页分区参数发成 dict；「待打包」撞名拆分 | — | `progress.md:4159` |
| R35 | v0.3.0rc1 · 2026-09-27 | 路径长度上限交给运行环境文件系统 | — | `progress.md:4217` |
| R36 | v0.3.0rc1 · 2026-09-27 | 真正安装 7-Zip 工具链；修两个只在 Windows 过的用例 | — | `progress.md:4271` |
| R37 | v0.3.0rc1 · 2026-09-28 | EH 原档偶发存成网页：链接认错 + 不验真身 | — | `progress.md:4310` |
| R38 | v0.3.0rc1 · 2026-09-29 | 验证码被判过期：取码与登录必须同一会话 | — | `progress.md:4367` |
| R39 | v0.3.0rc1 · 2026-09-29 | 来源按钮对已完成下载是空操作，重试不生效 | — | `progress.md:4408` |
| R40 | v0.3.0rc1 · 2026-09-29 | 仅 TG 账户也能自动摄取；下载来源优先级可配 | — | `progress.md:4466` |
| R41 | v0.3.0rc1 · 2026-09-30 | 合并候选撞外键导致服务起不来（AI 缓存级联） | — | `progress.md:4536` |
| R42 | v0.3.0rc1 · 2026-09-30 | MTProto 失败自述原因；重下失败不删旧原档 | — | `progress.md:4595` |
| R43 | v0.3.0rc1 · 2026-09-30 | 冷会话先补会话列表再解析频道 | — | `progress.md:4655` |
| R44 | v0.3.0rc1 · 2026-09-30 | 来源可读性只检测一遍，不在每轮重试 | — | `progress.md:4704` |
| R45 | v0.3.0rc1 · 2026-09-30 | 大文件下载改调真实 Telethon 接口，兜底自述 | — | `progress.md:4761` |
| R46 | v0.3.0rc1 · 2026-09-30 | 长传输独立客户端；Telethon 重试耗尽译成可重试原因 | — | `progress.md:4840` |
| R47 | v0.3.0rc1 · 2026-09-30 | 压缩率门禁只拦识别不出是图片的成员 | — | `progress.md:4925` |
| R48 | v0.3.0rc1 · 2026-09-30 | 7z/rar 按压缩块判比例，触发时才读成员头 | — | `progress.md:5003` |
| R49 | v0.3.0rc1 · 2026-09-30 | 来源规则可读账户会话列表，选一个预填表单 | 1575 | `progress.md:5077` |
| R50 | v0.3.0rc2 · 2026-10-01 | 彻底删除、解析规则页、AI 候选判定、来源批量 | 1650 | `progress.md:5146` |
| R51 | v0.3.0rc2 · 2026-10-01 | 内置解压器读不了的加密 ZIP 回退到 7-Zip；修旧式 `.rNN` 分卷发现与跨挂载打包；补齐 RAR 实测 | 1672 | `progress.md:5264` |
| R52 | v0.3.0rc2 · 2026-10-03 | 手机底栏直连（删抽屉）、卡片全量中文标签、来源纳入搜索 + 自定义排序、AI 总开关与每功能模型链 | 1690 | `progress.md:5356` |
| R53 | v0.3.0rc2 · 2026-10-03 | 移动端鉴权 B1：凭据表 `023`、密码登录换令牌、Bearer 通道、共享登录锁定 | 1728 | `progress.md:5461` |
| R54 | v0.3.0rc2 · 2026-10-03 | 移动端鉴权 B2：密码库单 API Key 面板（明文只显示一次）、令牌有效期可调 | 1741 | `progress.md:5518` |
| R55 | v0.3.0rc3 · 2026-10-04 | 发布：版本提升到 `v0.3.0rc3`，构建并推送 `latest` 镜像（无代码/测试变化） | — | `progress.md:5547` |
| R56 | 2026-10-04 | 清理收口：删死代码 `reference.py`、移除 `httpx2`、补 `THUMBNAILS_ENABLED`、AI 链错误码去路径化 | — | `progress.md:5569` |
| R57 | 2026-10-06 | 修复 Web 登录表单被 API 同名 `login` 路由劫持（R53 回归）：API 侧改名 `api_login`/`api_logout`/`api_batch_review` | 1741→1743 | `progress.md:5598` |
| R58 | 2026-10-06 | 自动审批规则增加「自动驳回」动作，与「自动通过」共用同一规则池与优先级 | 1743→1752 | `progress.md:5647` |

> 基线链（当前）：… → R50 1650 → R51 1672 → R52 1690 → R53 1728 → R54 1741 → R57 1743 → **R58 1752**。以 `AGENTS.md` 的链为准。

## 三、当前状态

- 最新阶段：**R58**（自动审批规则新增「自动驳回」动作；全量 1743 → 1752）。
- R58 给 `auto_approval_rules` 加 `action`（迁移 `024`，默认 `APPROVE` 回填既有规则）：命中「自动驳回」规则即置为已驳回（记 `filter_reason` 与 `AUTO_REJECT` 审计，可「重新排队」恢复），命中「自动通过」照旧入队；两类规则共用同一规则池与 `(priority, id)` 顺序，`apply_automatic_approval` 因此改名 `apply_automatic_decision`。设计记录 `AUTO_RULE_ACTIONS_PROPOSAL.md`，详见 `progress.md:R58`。
- R57 只改 API 侧同名处理函数的名字（`api_login`/`api_logout`/`api_batch_review`），页面侧 `login`/`logout`/`batch_review` 不变，URL 与模板一字未动；回归前 `url_for('login')` 指向 `/api/v1/auth/login`，网页登录因此报 `BODY_INVALID`。设计记录 `LOGIN_ROUTE_COLLISION_PROPOSAL.md`，详见 `progress.md:R57`。
- R56 清掉审阅发现的三处遗留（死代码 `app/candidates/reference.py`、dev 依赖 `httpx2`、
  `compose.deploy.yaml` 漏掉的 `THUMBNAILS_ENABLED`），并把共用的模型链失败码从 `AI_PATH_UNAVAILABLE`
  拆出中性的 `AI_CHAIN_UNAVAILABLE`（路径服务再翻译回原码，对外契约不变）；详见 `progress.md:R56`。
- 前序：**R55**（`v0.3.0rc3`，发布镜像；无代码改动，测试基线沿用 R54 的 `1741 collected / 0 failed`）。
- 已完成：R52 的手机底栏直连（删除二级抽屉）、候选/已下载卡片全量中文标签、来源纳入已下载搜索、
  `sort` + `dir=asc|desc` 自定义排序，以及 AI 控制链（`system_settings.ai_enabled` 总开关、
  每功能「跟随全局默认 / 本页单独指定」模型链、全部失败不回退、三处共用同一编辑器），
  设计记录见 `UI_QUERY_AND_AI_CONTROLS_PROPOSAL.md`；搜索与过滤的整块重构留给下一轮（方案 §3.3）。
- 前序：R50 的四项需求（彻底删除、解析规则独立页、AI 候选判定、来源批量），设计记录见
  `CANDIDATE_ADMISSION_AND_DELETE_PROPOSAL.md`；R51 让内置解压器读不了的加密 ZIP 自动回退到 7-Zip，
  设计记录见 `ENCRYPTED_ARCHIVE_FALLBACK_PROPOSAL.md`。
- R53/R54 一起构成**移动端鉴权**：`023` 凭据表（`api_key`/`access`/`refresh`，单 Key 部分唯一索引）、
  `/api/v1/auth/login|refresh|logout|whoami`、`require_session` 同时接受会话与 `Authorization: Bearer`、
  Bearer 豁免 CSRF；密码库页新增「API 密钥（移动端）」单 Key 面板，令牌有效期在「设置 › 系统」可调。
  设计记录见 `MOBILE_CLIENT_PROPOSAL.md`；客户端（M0–M4）在独立仓库 `ehbot-mobile` 实施。
- R55 是发布动作：版本提升到 `v0.3.0rc3` 并推送 `hsmk/ehbot:latest`（index digest `sha256:0216391e…`，amd64 manifest `sha256:a0631220…`）；镜像内经冒烟确认 R53/R54 的鉴权端点、`023` 迁移与单 Key 索引都已随发布就位，详见 `progress.md:5547`。
- 已知长期未验证项：1C512M 低资源档、真凭据 `docker compose up` 全链路、真机无障碍走查。
