# 同类项目竞品分析与重构依据

> 调研日期：2026-08-25。调研目的：为 EhBot 的 **UI 设计与流程控制重构** 提供事实依据。
> 结论先行：EhBot 的后端能力（来源链、归档流水线、安全门禁）已经超过多数同类项目，**问题完全集中在交互层与流程编排层**。因此重构的对象是 UI/UX 与流程控制，不是重写后端。

## 1. 调研对象

按「与 EhBot 的定位相关度」筛选，数据取自 GitHub API（stars 为调研当日值）。

| 项目 | Stars | 语言 | 定位 | 对 EhBot 的参考价值 |
|------|-------|------|------|---------------------|
| [gallery-dl](https://github.com/mikf/gallery-dl) | 19.3k | Python | 命令行画廊下载器，支持 E-Hentai | 元数据/下载适配器分层；无 UI 可参考 |
| [Sonarr](https://github.com/Sonarr/Sonarr) | 15.3k | C# | 剧集自动化 PVR | **流程控制黄金标准**：Activity/Queue/History/Wanted 四分区 |
| [Radarr](https://github.com/Radarr/Radarr) | 14.2k | C# | 电影自动化管理 | 同上；质量档（Quality Profile）模型 |
| [Kavita](https://github.com/Kareadita/Kavita) | 11.6k | C# | 阅读服务器，CBZ/ComicInfo/OPDS | **书库浏览与卡片式 UI 标杆**；元数据编辑弹层 |
| [Suwayomi-Server](https://github.com/Suwayomi/Suwayomi-Server) | 7.5k | Java/Kotlin | Tachiyomi 桌面/服务端版 | 下载队列与来源（Source）抽象 |
| [Prowlarr](https://github.com/Prowlarr/Prowlarr) | 7.1k | C# | 索引器管理/代理 | 来源健康度与测试连接的交互范式 |
| [Komga](https://github.com/gotson/komga) | 6.6k | Kotlin | 漫画媒体服务器，DDD 架构 | 书库/系列/书籍三层模型；REST API 优先 |
| [LANraragi](https://github.com/Difegue/LANraragi) | 3.1k | Perl | 归档型漫画阅读器，面向 doujinshi | **与 EhBot 内容域最接近**：标签体系、批量操作、插件式元数据补全 |
| [autobrr](https://github.com/autobrr/autobrr) | 3.0k | Go | 现代下载自动化（IRC/种子） | **过滤器（Filter）编辑器交互**是自动审批规则的最佳参考 |
| [mylar3](https://github.com/mylar3/mylar3) | 1.5k | Python | 漫画自动下载器 | 传统 *arr 式流程，反面教材偏多 |
| [Kapowarr](https://github.com/Casvt/Kapowarr) | 1.1k | Python | 漫画书库构建/管理，*arr 套件风格 | Python 实现的 *arr 流程，与 EhBot 技术栈最接近 |

## 2. 同类项目的共性设计（值得吸收）

### 2.1 流程控制：*arr 系的四分区模型

Sonarr/Radarr/Prowlarr/Kapowarr 一致采用的信息架构，是解决「任务散落在多个页面」的成熟答案：

| 分区 | 含义 | EhBot 对应 |
|------|------|-----------|
| **Library / Series** | 已入库的最终产物 | 书库（缺失，🚧） |
| **Activity → Queue** | 进行中的任务，含进度、速度、ETA | 下载任务页（有，但形态落后） |
| **Activity → History** | 已完成/失败的历史，可追溯、可重做 | 历史页（有，过于单薄） |
| **Wanted / Missing** | 待处理、缺信息、失败需干预 | 候选队列 + 待补充 + 失败（**被拆成 3 个独立页面**） |
| **Settings** | 分组的设置中心，单页多 Tab | 归档设置/来源规则/自动审批/外部连接（**被拆成 4 个顶级导航**） |

**关键差异**：*arr 把「同一心智任务」聚合到一个页面的多个 Tab 下，EhBot 把它们平铺成了 9 个顶级导航项。

### 2.2 交互层：这些项目都不用整页刷新

- Kavita/Komga/autobrr/Suwayomi 均为 **SPA + REST/JSON API**，进度通过轮询 JSON 或 WebSocket 局部更新。
- Sonarr/Radarr 用 SignalR 推送队列变化，页面无刷新。
- **无一个使用 `<meta http-equiv="refresh">` 整页刷新**——而这正是 EhBot 当前的「实时进度」实现方式。

### 2.3 内容展示：封面驱动的卡片/网格

- Kavita、Komga、LANraragi、Suwayomi 的主界面都是**封面缩略图网格**，标题为辅、封面为主。
- 提供网格/列表视图切换、排序、筛选侧栏、批量多选工具条。
- **EhBot 目前完全没有封面**：候选队列是纯文字表格行，`thumb` 字段虽已从 gdata 解析但从未展示。

### 2.4 规则/过滤器编辑：autobrr 范式

- autobrr 的 Filter 编辑器：分组条件、下拉+输入组合、实时匹配预览、可对历史数据「试跑」。
- LANraragi 的批量标签操作：多选 + 工具条 + 即时反馈。
- **EhBot 当前**：单一 `Regex({Field}, 'pattern')` 文本框，无试跑、无命中预览、无组合条件。

### 2.5 元数据编辑：就地编辑 + 字段级来源标注

- Kavita/Komga：弹层内分 Tab 编辑，字段显示「来自刮削器/手动覆盖」，可锁定字段防止被覆盖。
- **EhBot 已有** `value_source` / `is_manual` 数据基础，但 UI 未体现「字段锁定」与「来源标注」。

## 3. EhBot 现状诊断（重构靶点）

基于对 `app/web/templates/`（13 个模板）、`app/web/static/app.css`（21KB、189 个类）、`app/main.py`（88KB、59 个路由）的审视：

### 3.1 UI 设计问题

| 编号 | 问题 | 证据 |
|------|------|------|
| U1 | **无封面视觉**，全站纯文字表格 | 所有模板无 `<img>` 内容图；gdata `thumb` 已解析未用 |
| U2 | **导航平铺 9 项**，无信息层级 | `base.html` 侧栏 9 个同级 `nav-link` |
| U3 | **移动端是另一套导航**，9 个横向链接堆叠 | `base.html` `.mobile-header` 重复定义导航 |
| U4 | **「队列」实为伪表格**，用 `div` + CSS grid 模拟，无排序/筛选/分页 | `.queue-table` / `.queue-header` / `.queue-row` |
| U5 | **状态仅为纯文本**，无统一徽章语义 | `downloads.html` 直接输出 `{{ job.state }}` 原始英文枚举 |
| U6 | **批量操作弱**：仅候选页有 checkbox，无全选/反选/浮动工具条 | `candidates.html` `batch_enabled` |
| U7 | **无 Toast/无确认弹层**，所有反馈靠整页跳转 + `error` 变量 | 各模板 `{% if error %}` |

### 3.2 流程控制问题

| 编号 | 问题 | 证据 |
|------|------|------|
| F1 | **「实时进度」是整页刷新** | `downloads.html:17` `<meta http-equiv="refresh">` |
| F2 | **几乎没有 JSON API**，无法做局部更新 | `app/main.py` 仅 `/api/connections/status` 一个 |
| F3 | **候选状态散落 4 页**，需手动在页面间跳转 | `/candidates`、`/needs-info`、`/processing`、`/failed` |
| F4 | **设置散落 4 个顶级页** | `/sources`、`/auto-approval-rules`、`/archive-settings`、`/connections` |
| F5 | **打包状态混在下载页**，provider 复用 `CONVERSION` 塞进 `download_jobs` | `downloads.html` `pack_jobs` 区块 |
| F6 | **前端零状态管理**，唯一 JS 是 16 行 DSL 预览 | `app/web/static/auto_approval.js` |
| F7 | **无书库页面**，CBZ 落盘即脱管，`{category}/{artist}/{title}` 布局未实现 | 无对应路由；`task_plan.md` 遗留项 |
| F8 | **`main.py` 88KB 单文件承载 59 路由**，UI 与业务逻辑耦合，难以重构 | `app/main.py` |

### 3.3 后端资产（重构中必须保留）

以下能力经 439 项测试覆盖，属于项目的真实价值，重构 **不得破坏**：

- 来源降级链 `TELEGRAM → EH_TORRENT → TELEGRAPH`（+ 手动 `EXHENTAI`）。
- 归档流水线：分卷检查 → 密码尝试 → 安全门禁 → 解压 → 打包 → 原子发布。
- 安全门禁：路径穿越、压缩炸弹、SSRF、图片魔数、符号链接、嵌套包。
- 7-Zip 托管安装（GitHub Release + SHA-256 + 版本隔离幂等）。
- EhTagTranslation 中文标签翻译（ETag 条件请求 + 本地缓存降级）。
- 幂等契约：`idempotency_key`、唯一约束、重启恢复、租约工作器。
- 凭据隔离：`data/private`、加密密码库、日志脱敏。

## 4. 重构结论

| 维度 | 决策 |
|------|------|
| 后端服务层（`app/archive`、`app/exhentai`、`app/torrent`、`app/telegraph`、`app/conversion`、`app/downloads`） | **保留**，仅补 JSON API 与状态上报 |
| `app/main.py` | **拆分**为 `app/api/`（JSON）+ `app/web/`（页面壳），按域分路由模块 |
| 前端 | **重写**：封面驱动、局部更新、统一组件语义 |
| 信息架构 | **重组**：9 个平铺导航 → 5 个域（工作台/候选/活动/书库/设置） |
| 实时机制 | `<meta refresh>` → **JSON 轮询 + SSE**（完成即推送） |
| 数据模型 | 增量扩展：封面缩略图、书库条目、字段锁定；不做破坏性迁移 |

详细的目标形态与实施计划见 `EHBot.md`（需求）与 `DEVELOPMENT_PLAN.md`（开发计划）。