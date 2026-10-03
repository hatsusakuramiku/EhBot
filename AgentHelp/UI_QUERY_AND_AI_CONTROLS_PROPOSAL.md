# 方案（合并）：界面导航 / 卡片标签 + AI 控制链

> 状态：**已实施（R52，2026-10-03）**。审阅通过后开工；`progress.md` 已追加 R52 条目，
> `PHASES.md` 已追加一行。本文件保留为设计记录。除 §3.3 明确留给下一轮的搜索/过滤重构外，
> §1–§4 均已落地（无数据库迁移，`022` 仍是最新）。
>
> 需求来源（运营者原话）：
>
> 1. 非桌面版本已下载页面为啥要再二级菜单选分区，这个明显没有必要；
> 2. 候选与作品查询页面作品卡片信息太少，应当展示所有中文标签；
> 3. 搜索过滤功能要调整，但那是下一个目标，**本次不考虑**；本轮只加来源与自定义排序；
> 4. 调整 AI 配置：供应商页面配全局默认模型，各使用 AI 的页面各自配模型，未配置才回退全局默认；
> 5. 「调用失败不自动回退」= 某功能配置的**所有**模型都失败时，不回退调用全局默认模型；
> 6. 全局提供一个 AI 功能开启，各功能再各自配置是否开启；所有模型配置都是「主力 + 备用」，
>    全局一份、各功能自配一份，配置方式保持统一。

## 0. 需求与落点

| 组 | # | 需求 | 落点 |
|----|----|------|------|
| 界面 | 1 | 非桌面版不再用二级菜单选分区 | §1 `base.html` 手机 tab bar；候选页页头补「手动添加」 |
| 界面 | 2 | 卡片展示全部中文标签 + 信息补足 | §2 `candidates.html`、`downloaded.html`、DTO、SQL（无迁移） |
| 界面 | 3 | 来源纳入搜索 + 自定义排序（搜索与过滤重构本轮暂缓） | §3 `database.py`、`ui.js`、两个列表页 |
| AI | 4 | 全局默认 + 每功能自配「主力 + 备用」 | §4.2–4.4 |
| AI | 5 | 全部失败不回退全局默认 | §4.5 |
| AI | 6 | 全局总开关 + 每功能开关 | §4.6 |

无数据库迁移（`system_settings` / `archive_settings` 都是 k/v 表），`022` 仍是最新。

---

## 1. 需求 1：非桌面版的「二级菜单选分区」

### 1.1 现状（证据）

手机（≤640px）导航在 `app/web/templates/base.html:158` 起的 `.ui-tabbar`：

- 有 `children` 的域（候选/活动/已下载/设置）渲染成 **`<button @click="drawer = true">`**，
  只有叶子（工作台/日志）是 `<a>`；
- 点开弹出 `role="dialog"` 的底部抽屉（`aria-label="分区导航"`，`base.html:180`），
  内容是**所有域的全部子页平铺**（`{% for item in nav_items %}{% for child in item.children %}`），
  列表里混着「候选·待审核」「活动·队列」「已下载·已打包」「设置·归档」……；
- 四个有子页的域，落地页**自己已经有分区条**：`candidates.html:95`、`activity.html:160`、
  `downloaded.html:117`、`settings.html:45` 都调 `ui.tabs(...)`，手机上同样渲染、同样可横向滚动。

于是手机上到「已下载」要：**底栏按钮 → 二级抽屉（混着所有域的分区）→ 选分区**，
落地后页面顶部又有一条一模一样的分区条。抽屉这一跳是纯冗余。

### 1.2 方案 A（已确认）

1. 手机 tab bar 与桌面侧栏同构：每个 item 都是 `<a href="{{ item.path }}">`，
   `aria-current` / `is-active` 判定不变（`NavItem.is_active` / `is_current` 已够用）。
2. **删除抽屉**：`x-data`、`@click="drawer = true"`、`<template x-teleport>` 与整段
   `.ui-drawer` 标记一并移除。
3. **唯一孤儿「手动添加」**（候选域子项，但不在候选 6 个状态分区条里）：在候选页（六个 tab 共用）
   页头加「＋ 手动添加」动作链接到 `/manual-add`。NAV_ITEMS 保留，桌面侧栏不变。

逐个核对，四域子页在手机上都可达：候选 6 个走页内分区条、手动添加走页头；活动 3 个、已下载 5 个、
设置 9 个都走页内分区条。

### 1.3 影响与测试

- `base.html` 手机 tab bar 重写；`ui.css` 的 `.ui-tab` 规则对 `a` 已适用（`text-decoration: none` 已有）。
- **必须改的测试**：`tests/integration/test_ui_shell.py:96`
  `test_the_two_navigations_render_the_same_destinations` 现在把所有域的子页 href 断言在
  `/candidates` 一个页面上——它成立正因为抽屉平铺了全部子页。改为更强的不变式：
  1. 桌面侧栏与手机 tab bar 的**顶级**链接集合相同；
  2. **每个域首页**上都能到达该域的全部子页（页内分区条或页头按钮）；
  3. `/manual-add` 在 `/candidates` 上可达。
- `test_every_page_marks_exactly_one_destination_as_current` 应继续通过。

## 2. 需求 2：卡片展示全部中文标签

### 2.1 现状（证据）

- 候选列表：`candidates.html:76` — `item.tags[:6]`，只显示 6 个。
- 候选网格：`candidates.html:185` — `tags=item.tags[:4]`，只显示 4 个。
- 已下载（作品）卡片**完全没有标签**：`downloaded.html:193` 调 `ui.cover_card(...)` 没传 `tags`；
  根因在数据层——`_DOWNLOADED_SELECT`（`database.py:145`）没取 `Tags`，
  `downloaded_work()`（`serializers.py:326`）也不输出 `tags`。

`item.tags` 就是**中文标签**：`app/exhentai/enrich.py:42` 把上游 `raw_tags` 经 `TagTranslator`
译成中文写入 `Tags`，英文原串另存 `TagsRaw`；`candidate_summary`（`serializers.py:85`）两者都发。

### 2.2 方案

1. **候选列表**：删除 `[:6]`，全量渲染 `item.tags`（`.ui-card-tags` 已是 `flex-wrap: wrap`）。
2. **候选网格**：`tags=item.tags`（去掉 `[:4]`）。
3. **作品（已下载）**：
   - `_DOWNLOADED_SELECT` 增加 `Tags`、`TagsRaw` 两个相关子查询（与候选列表同款）；
   - `DownloadedWork`（`app/downloads/models.py:208`）加 `tags` / `raw_tags`；
     `_downloaded_work`（`database.py:183`）按新列序映射；
   - `serializers.downloaded_work` 输出 `"tags"` / `"raw_tags"`（复用 `_split_tags`）；
   - `downloaded.html` 网格 `ui.cover_card(..., tags=item.tags)`；列表在标题下加同一行标签。
4. **信息补足**：网格卡片在 meta 行下加「来源 · 页数 · 大小」（复用 `size_text` 宏），
   新增 `.ui-card-facts` 样式。
5. 不折叠、不加「展开」。必要时 `.ui-cover-grid { align-items: start; }` 让卡片各按自身高度。

### 2.3 性能

每行多 2 个相关子查询；`_DOWNLOADED_SELECT` 现有 4 个、候选列表现有 4 个，页面固定 50 行，
代价同数量级，不需要改查询结构；标签是纯文本 span，相比 50 张封面图可忽略。

## 3. 搜索与排序：本轮只收敛为两项

> 运营者 2026-10-02：「搜索过滤功能我考虑要调整一下……这是下一个目标了，本次不考虑」。
> 所以把原先的「查询与过滤完善」整块**移出本轮**，只保留两个明确点名要的增补；
> 其余进入下一轮「搜索与过滤重构」（§3.3）。已下载的 facet 侧栏、facet 交互重做、
> 筛选 chips 都随之暂缓。

### 3.1 来源（provider）纳入已下载搜索

`_list_downloaded_works_sync` 的搜索条件（`database.py:1832-1839`）现在只匹配
`Title`/`JapaneseTitle`/`Artist`/`Group`/`Tags`/`TagsRaw`。增加按**来源**匹配：
`download_jobs.provider` 的 code 与 `provider_label()` 的中文名（如「Telegram 用户」「ExHentai」）
都能命中，与现有字段是 OR 关系。只影响 `/downloaded`（候选不是下载任务，没有来源）。

### 3.2 自定义排序

- 两个列表页都支持 `sort` + **`dir=asc|desc`**：`_CANDIDATE_SORTS` / `_DOWNLOADED_SORTS`
  的每条 ORDER BY 生成正反两种；`dir` 进 URL，链接与书签可复现。
- 工具栏排序旁加 ↑/↓ 控件（纯 `<a>` 链接，服务端渲染，无 JS 也能用）。
- 顺手修一个真实 bug：`downloaded.html:136` 的 `data-autosubmit` 是**死属性**——处理逻辑只在
  `candidates.js:164`。把这段提到 `ui.js`（全局、幂等），两个列表页的排序控件都即时生效；
  `downloaded.js` 不动，无 JS 时「应用」照旧。

### 3.3 下一轮：搜索与过滤重构（本轮暂缓，记录已定决策）

移出本轮、留给下一轮重新设计的内容：

- 已下载页的 facet 侧栏（标签/作者/语言/分类/来源）；
- facet 的「显示更多」「组内过滤」「选中值常驻」「渲染上限 24 → 200」；
- 可点掉的筛选 chips；
- 搜索补 `ArtistRaw`/`GroupRaw` 与归档文件名 / 库内相对路径。

下一轮设计时沿用本轮的结论：状态全在 query string、复用 `CANDIDATE_FACETS` 与
`_FACET_CONTAINS_SQL`、页面与 `/api/v1/downloaded` 共用同一个 snapshot。

---

# 第二部分：AI 控制链

## 4. AI 控制链

### 4.1 现状（证据）

- **全局默认模型**已在 `/settings/ai`：`_ai.html:18-32`，一条有序列表（第 0 位主力，其余备用），
  作用域 `CHAIN_SCOPE_DEFAULT`（`app/ai/models.py:26`），表 `ai_model_chain`（migration 018）。
- **每功能自配**目前只有归档路径页：`/settings/paths`「路径决策模型」（`_paths.html:159-190`），
  单选 `ai_model_source` = `default`/`custom`（`archive/service.py:116-120`、`611-624`），
  作用域 `CHAIN_SCOPE_ARCHIVE_PATH`。
- **AI 候选判定没有模型配置**：`app/candidates/admission.py:90`、`:98-103` 写死
  `effective_chain(CHAIN_SCOPE_DEFAULT)` / `complete(scope=DEFAULT)`；`_parse.html` 只有开关、
  兜底动作、覆盖开关、提示词。
- **作用域解析硬编码**：`AiProviderService.effective_chain()`（`ai/service.py:597-621`）里
  `if scope == CHAIN_SCOPE_ARCHIVE_PATH:` 直接读 `ai_model_source`，加第三个作用域无处可加。
- **失败语义**：`complete()`（`ai/service.py:781`）进来先 `effective_chain(scope)` 解析一次，
  之后只在这条链里循环（主力→备用）；全部失败抛错。**不跨作用域回退**——已经成立，只是没有测试、
  日志与文案锁住。
- **开关**：只有功能级——`ai_candidate_enabled`（`system_settings`，`settings/service.py:47`）、
  `path_source == "ai"`（归档设置）。**没有全局总开关**。

### 4.2 目标行为（规范）

- **A1 全局一份**：全局默认模型 = 一条「主力 + 备用」列表，配在 `/settings/ai`。
- **A2 每功能一份**：每个使用 AI 的功能页有自己一条「主力 + 备用」列表，
  用**同一个编辑器、同一套动作**（设为主力/上移/下移/移出/加入备用），只是挂在自己的 URL 下。
- **A3 未配置才回退**：该功能未单独配置（来源 = 跟随全局默认）→ 用全局那份；
  选「本页单独指定」→ 只用本页那份（列表为空是显式报错，不静默回退）。
- **A4 失败不回退**：本功能配置的**所有**模型都失败 → 直接失败，**不回退调用全局默认模型**；
  失败交给该功能自己的兜底策略（路径页 `ai_fallback_to_rules`；候选页「拒绝/放行」）。
- **A5 开关链**：全局一个「AI 功能总开关」，各功能再各自有开关。
  **生效 = 总开关 AND 本功能开关**；总开关关闭时，任何功能都不产生 AI 调用。
- **A6 可见**：每个功能页显示控制链当前状态（总开关 / 本功能开关 / 当前生效模型 / 「失败不回退」）。

### 4.3 作用域与配置存储（无迁移）

`app/ai/models.py`：

```python
CHAIN_SCOPE_DEFAULT = "default"                    # 全局默认
CHAIN_SCOPE_ARCHIVE_PATH = "archive_path"          # 归档路径
CHAIN_SCOPE_CANDIDATE = "candidate_admission"      # AI 候选判定（新增）
CHAIN_SCOPES = (DEFAULT, ARCHIVE_PATH, CANDIDATE)
CHAIN_SCOPE_LABELS = {"default": "全局默认", "archive_path": "归档路径",
                      "candidate_admission": "AI 候选判定"}
```

「模型来源」单选（`default`/`custom`）与 `MODEL_SOURCES` 从 `app/archive/service.py` 提到
`app/ai/models.py`（现在放在归档服务里，候选判定引用会形成怪依赖）：

| 作用域 | 来源开关存储 | 值 |
|--------|--------------|----|
| `archive_path` | `archive_settings.ai_model_source`（现状不变） | `default` / `custom` |
| `candidate_admission` | `system_settings.ai_candidate_model_source`（新增键） | `default`（缺省）/ `custom` |

缺省即 `default` = 跟随全局 = **升级后行为与今天完全一致**。

### 4.4 解析改成注册表（删掉硬编码）

`AiProviderService.__init__` 现在收 `archive_settings`；改为收
`scope_sources: Mapping[str, Callable[[], Awaitable[str]]]`（或 `register_scope_source()`），
`wiring.py:539` 注册 `archive_path → archive_settings.ai_model_source`、
`candidate_admission → system_settings.ai_candidate_model_source`。`effective_chain(scope)`：

```python
if scope == CHAIN_SCOPE_DEFAULT:  return 本作用域链
reader = self._scope_sources.get(scope)
if reader is not None and await reader() != MODEL_SOURCE_CUSTOM:
    return 全局链                      # 未配置 → 回退全局（只在调用开始前发生一次）
return 本作用域链                       # 已配置 → 只用它；为空则显式报错
```

### 4.5 失败语义（需求 5）

`complete(scope=...)` 保持「只在解析出来的那条链里走」；链内主力→备用是**显式配置**的备用，保留。
某条链全部失败时抛错，**不查全局链**。加强可观测性：

- 错误文本带作用域标签（`CHAIN_SCOPE_LABELS`），例如
  「AI 候选判定：配置的 2 个模型都失败（未回退全局默认）：…」；
- 日志 `ai_model_failed` / `ai_path_model_failed` 增加 `scope` 字段；
- 用回归测试钉死（§5）。

### 4.6 开关链（需求 6）

- **新增全局总开关**：`system_settings.ai_enabled`（`SystemSettingsService` 读写，缺省 `1` 开）。
- **现有功能开关**：归档路径 `path_source == "ai"`；AI 候选判定 `ai_candidate_enabled`。
- **生效规则** = 总开关 AND 本功能开关：
  - 总开关关 + 归档路径为 AI 模式 → 等同未启用 AI 模式，按模板/规则归档，**不产生 AI 调用**；
  - 总开关关 + 候选判定开关开 → `decide()` 返回「跳过：AI 功能已全局关闭」，消息按解析规则处理，
    **不产生 AI 调用**（与 R50「AI 判定不是必选项」一致）；
  - 总开关开 → 完全由各功能自己的开关决定。
- `/settings/ai` 顶部新增「AI 功能总开关」区块，文案写清整条控制链：
  `总开关 → 各功能开关 → 该功能模型（本页主力+备用；未配置则全局默认主力+备用）→ 全部失败不回退`。
- 功能页（`/settings/paths`、`/settings/parse`）的 AI 区块显示：
  「总开关：开/关 · 本功能：开/关 · 当前模型：跟随全局默认（主力 X）/ 本页指定（主力 A → 备用 B）·
  失败不回退全局默认」。
- 「测试模型」按钮**不受总开关影响**（验证模型是显式操作，也是开启前的准备动作）。

### 4.7 统一配置方式

三处（全局、路径、候选判定）共用同一个 `settings/_model_chain.html` 宏与同一套链动作路由
（`<endpoint>/primary|append|shift|remove`）：全局 `/settings/ai/chain`、路径
`/settings/paths/chain`、候选判定 `/settings/parse/chain`（新增）。**同一份编辑器、同一套按钮、
同一条「主力 + 备用」语义**，不存在两套规则。

## 5. 测试计划

**AI（重点锁住 A3/A4/A5）**

- 单元：`effective_chain` 每个作用域「未配置 → 全局链」「custom → 自己的链」「custom 为空 → 空链（不继承）」。
- **回归（直接对需求 5）**：候选作用域配置一个只会失败的模型 A，全局默认是一个会成功的模型 B；
  断言最终**失败**，且 B **从未被调用**（假 client 记录调用）。归档路径作用域同样一条。
- 开关链：总开关关 → 候选判定跳过、路径 AI 落规则，且假 client **零调用**；总开关开 + 功能关 → 零调用；
  两者都开 → 才调用。
- 集成：`/settings/parse` 保存模型来源与链顺序；`/settings/paths` 现有用例全绿（回归红线）；
  `/settings/ai` 保存总开关；页面显示控制链状态。

**界面**

- `test_ui_shell.py`：按 §1.3 重写；新增「手机 tab bar 无 `role="dialog"` 抽屉、有子页的域是 `<a href>`」。
- `test_candidates_web.py`：>6 个标签全量出现在列表与网格。
- `test_downloaded_web.py`：卡片带标签与「来源·页数·大小」；`sort`+`dir` 生效；
  来源命中搜索；`data-autosubmit` 属性存在且 `ui.js` 提供处理。
- `test_api_domains.py`：页面 context ⊇ JSON body（现有断言）。

## 6. 文档同步

- `README.md`：AI 控制链（总开关 / 各功能模型 / 回退规则）与界面导航描述。
- `docs/USAGE.md`：§Web 界面（域/分区表）、§已下载内容（来源搜索、排序方向、卡片标签）、
  §解析规则与 AI 候选判定（新增模型配置）、§路径来源与 AI 路径、§AI 供应商（总开关）。
- `AgentHelp/EHBot.md`：§1.3.1 与 AI 相关段落。
- `AgentHelp/progress.md`：R52 条目（版本、测试基线、无迁移）；`PHASES.md` 一行 + 基线链。

## 7. 风险

| 风险 | 处理 |
|------|------|
| 删抽屉弱化 R3「一份数据源」不变式 | 用更强的「每个域首页可达其全部子页」替代（§1.3），是收紧不是删除 |
| `effective_chain` 是路径与候选判定的公共核心路径 | 通用化前后各加覆盖两种作用域的用例；`/settings/paths` 现有用例全绿为红线 |
| 全局总开关误挡已在运行的 AI | 缺省开；总开关关闭只让功能"不调用"，不删任何配置 |
| 搜索与过滤重构延后，下一轮之前过滤仍显不足 | 本轮先交付明确点名的两项；下一轮按 §3.3 重新设计 |
| 卡片标签行高不一 | `.ui-cover-grid` 按行等高，必要时 `align-items: start` |

## 8. 实施顺序

1. §1 导航（模板 + `test_ui_shell` 重写）；
2. §2 标签与卡片信息；
3. §3.1–3.2 来源搜索 + 自定义排序（含 `data-autosubmit` 修复）；
4. §4 AI 控制链（作用域通用化 → 候选判定配置 → 总开关 → 错误/日志/页面）；
5. 全量测试；文档同步；构建并推送 `hsmk/ehbot:latest`（不提交）。

## 9. 待确认项

无。你 2026-10-02 的口径已全部落入方案：来源纳入搜索、加自定义排序、搜索与过滤重构整块
留到下一轮；AI 侧为主力+备用、配置方式统一、未配置才回退全局、全部失败不回退、
全局总开关 + 各功能开关。
