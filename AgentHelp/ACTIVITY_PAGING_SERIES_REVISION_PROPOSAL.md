# 方案：活动页标题 + 每页条数 + 系列元数据 + 移除「要求修订」（R81–R84 候选，2026-10-11）

**状态：待审阅 v3（2026-10-11）。** 已获 go-ahead，R81 起开工。
v2 收入第 1、2、3 问的答复（§0.1）；v3 依运营者「历史的要求修订也不要保留，完全移除」把审计行也改成删除。

> ⚠️ **并行会话提醒。** 本仓库当前有另一个会话在推进（R64–R80 移动端 API 补口 + 设置写入，
> 已提交到 `origin/main`，最新 `ad7d8a3`）。本方案的四项都落在它刚动过的区域：
> 需求 1 碰活动模板、需求 3 碰打包/AI 路径、**需求 4 直接删掉 R70 刚加的
> `POST /api/v1/works/{id}/needs-revision`**。开工前需要确认那个会话已经收工，否则会互相踩。

关联：`AgentHelp/AGENTS.md`（先方案后开工；文档是改动的一部分）、`CANDIDATE_TITLE_PROPOSAL.md`（R62，
`title_subquery()` / `display_title`）、`MOBILE_API_COMPLETION_ROUND2_PROPOSAL.md`（R70，要求修订的 API）。
阶段号：建议 **R81（需求 1）/ R82（需求 2）/ R83（需求 3）/ R84（需求 4）**；**需求 4 需要一条数据迁移 `025`**。

---

## 0.1 已定结论（v2）

| # | 问题 | 结论 |
|---|------|------|
| 1 | 活动页「标题」怎么改 | **可**（主行改作品名，`任务 #<id>` 降副行，候选链接挂作品名） |
| 2 | 「系列相关字段」 | **新开 `Series` 元数据字段**（`Parody` 不动，仅当没有 `Series` 时才回落到今天的 `parody or title`） |
| 3 | 开了「自动添加系列」但 AI 不可用 | **首次打包 → 任务进「需干预」（可重试）；重新打包 → 先提示、再中止本次重打包**（不替换现有 CBZ）——细则见 §2.3 |

---

---

## 0. 需求（运营者原话，2026-10-11）

1. 活动页面各卡片标题还是 ID，也要改成标题。
2. 候选与下载页面，需要可自定义每页的作品数，不要固定限制为 50。
3. 打包时在元数据里加系列（Series）（路径设置页加一个「自动添加系列」设置项，仅 AI 可用时才有效）：
   元数据有系列相关字段就用它，没有就交给 AI — 若用 AI 设定的归档路径，则打包路径所在的文件夹就是系列名；
   若非 AI 设定的归档路径、但打包归档路径里配了 AI 并且已开启，就通过 AI 解析系列；未开启则不加（元数据没有时）；
   开启了但最后 AI 不可用则打包失败。重新打包也要同样处理。
4. 完全移除审核里的「要求修订」功能，一点都不要留。

---

## 1. 现状证据

### 1.1 需求 1 — 活动页的主标题是任务 ID

- 行/卡片的主行是**任务 ID**：`app/web/templates/activity.html:52` `<span class="ui-table-primary">#{{ job.job_id }}</span>`，
  候选列是 `app/web/templates/activity.html:62` `<a href="{{ job.href }}">候选 #{{ job.candidate_id }}</a>`。
- 载荷 `job_summary()`（`app/api/serializers.py:357`）里有 `candidate_id`、**没有 title**；
  DTO `DownloadJobSummary`（`app/downloads/models.py:222` 一带）也没有；
  `_fetch_jobs()` 的 SELECT（`app/downloads/service.py:835/841`）同样没取标题。
- 标题解析已经有唯一真源 `title_subquery(candidate_ref)`（`app/db/database.py:243`）与显示兜底
  `display_title(title, ex_gid, candidate_id)`（`app/candidates/models.py`，R62）。活动页是**唯一**没接上的地方。

### 1.2 需求 2 — 后端已支持 `page_size`，页面不给选

- `DEFAULT_PAGE_SIZE = 50`、`MAX_PAGE_SIZE = 200`（`app/api/contracts.py:26-28`）；
  `PageParams.clamp()`（`:103`）已经把 `?page_size=` 夹在 `[1, 200]`。
- 两个网页路由确实读了这个参数（`app/web/routes/candidates.py:167`、`app/web/routes/downloaded.py:132`），
  并把 `page_size` 交给模板（`candidates.py:246`、`downloaded.py:170`）。
- 但模板只渲染「上一页 / 下一页」（`ui.pagination`，`app/web/templates/components/ui.html:161`；
  用在 `candidates.html:289`、`downloaded.html:267`），**没有任何地方能改每页条数** ——
  于是实际永远是默认 50。API 侧本来就能传，缺的只是网页控件。

### 1.3 需求 3 — ComicInfo 的 `<Series>` 现在是「原作或标题」

- `app/conversion/comicinfo.py:49-50`：`<Series> = parody or title`、`<LocalizedSeries> = japanese_title or title`。
  这是需求要动的地方，但注意它现在**已经在写 `<Series>`**，只是值来自 `Parody`，且没有独立的系列概念。
- `metadata_values` 没有 `Series` 字段（`app/review/models.py:87` 的 `METADATA_FIELDS`）；
  表结构对字段名无约束，只有 `UNIQUE (candidate_id, field_name, value_source)`（`app/migrations/001_initial.sql:57`），
  所以**加一个 `Series` 字段不需要迁移**。
- ComicInfo 从哪里来：`ConversionService._build_comicinfo()`（`app/conversion/service.py:1333`）把
  `_metadata_lookup(metadata, ...)` 逐字段喂给 `build_comicinfo_xml()`；它在 `:1108` 附近被当作
  `comicinfo_builder=lambda page_count: ...` 传进 `processor.process(...)`，**同步、在 `to_thread` 里跑**——
  所以任何要问 AI 的步骤必须在 `process()` 之前、在异步侧算好。
- AI 路径侧：AI 建议已经有「目录」这一级 —— `AiPathSuggestion.directory`（`app/ai/models.py:311`），
  `path_source == ai` 的分支在 `app/conversion/service.py:440` 用它拼最终路径。也就是说
  「AI 路径所在的文件夹」**现成可读**，不需要再问一次模型。
- 「AI 可用」的既有语义：全局 `system_settings.ai_enabled`（`app/settings/service.py:326`）
  ＋ 该功能作用域的可用模型链；AI 路径还额外要求 `path_source == ai`（`app/archive/service.py:91`）。
- 重新打包与首次打包走的是同一条打包代码路径（`enqueue_for_candidate` → 同一个 worker），
  所以只要改在打包路径里，重新打包自然一起生效 —— 需要在测试里钉住这一点。

### 1.4 需求 4 — 「要求修订」的触点（全部要清）

| 层 | 位置 |
|---|---|
| 领域模型 | `app/review/models.py:8` `REVIEW_NEEDS_REVISION`、`:44` `STATUS_NEEDS_REVISION`、`:52/:68` 两个状态集 |
| 服务 | `app/review/service.py:66` `request_revision()`（含 `REVISION_REASON_REQUIRED`） |
| JSON API | `app/api/actions.py:756` `POST /works/{id}/needs-revision`（**R70 刚加**）；`app/api/works.py:146` `needs_revision` 动作位 |
| 状态词表 | `app/api/status.py:81`、`:361` 两条视图 |
| 数据库 | `app/db/database.py:88` 状态词映射、`:2201` 「待补充」查询把 `NEEDS_REVISION` 并进 `NEEDS_INFO` |
| 网页 | `app/web/routes/candidates.py:611` 路由；`app/web/templates/work_detail.html:420-421` 按钮 |
| 其他消费方 | `app/exhentai/scrape_worker.py:65`（抓取状态集）、`app/conversion/service.py:61/86`（打包拒绝的未定夺态）、`app/downloads/service.py:266`、`app/api/candidates.py:40`、`app/web/routes/ui_kit.py:28` |
| 测试 | `tests/integration/test_api_domains.py` 的 `TestNeedsRevision`（5 条，R70）＋各处的 revision 断言 |

- `candidates.status` 是**裸 TEXT、没有 CHECK 约束**（`001_initial.sql:39-49`），所以移除它不需要重建表，
  但库里可能已有 `status='NEEDS_REVISION'` 的**存量行**必须处理（见 §4）。
- 历史审计 `review_actions.action='NEEDS_REVISION'` 也已经存在 —— 运营者定稿：**一并删除**（「完全移除，
  一点都不要留」），迁移 `025` 里 `DELETE FROM review_actions WHERE action='NEEDS_REVISION'`。

---

## 2. 设计

### 2.1 需求 1（R81）：活动页主标题改为作品名

- `DownloadJobSummary` 增 `title: str | None` 与 `display_title: str`；
  `_fetch_jobs()` 的 SELECT 增一列 `title_subquery("download_jobs.candidate_id")`（与全站同一套解析，不另立一份）。
- `job_summary()` 增 `title` 与 `display_title`（用 R62 的 `display_title(title, ex_gid, candidate_id)` 兜底，
  无标题的候选仍然显示 `#<gid>` / `候选 #<编号>`，而不是空白）。
- 模板：行/卡片的**主行改成作品名**，任务 ID 降为副行（`任务 #<job_id>`），候选链接改挂作品名
  （`href` 不变，仍是 `/works/<id>`）。这样移动端卡片与桌面表格共用一份，不新增第二套标记。
- 网格/列表两种视图都改；`activity.js` 只按 `data-field` 打补丁、不构造行，所以不需要动它
  （但若给标题加了 `data-field`，要确认轮询回来时不会被空值覆盖 —— 这一列不会变，可不加）。

### 2.2 需求 2（R82）：每页条数可选

- 加一个**链接式**的每页条数选择器（和排序控件同一套路：写进 URL、无需 JS、可分享/收藏）：
  `?page_size=20|50|100|200`，当前值高亮。
- 复用 `deps.query_href(request, page_size=...)` 生成链接，同时把 `page` 归零（换条数回到第 1 页）。
- 选择项与默认值见 §3 待确认（建议 `20 / 50 / 100 / 200`，默认仍是 50，`MAX_PAGE_SIZE` 保持 200 ——
  这是一个有界扫描，不建议放开到无上限）。
- 只动 `candidates.html` / `downloaded.html` 与 `ui.pagination` 附近；活动页是实时快照、不分页，不在范围内。
- API 侧现状不变（本来就支持 `page_size`）。

### 2.3 需求 3（R83）：系列元数据

新增一个可编辑、可锁定的元数据字段 `Series`，并让打包把它写进 ComicInfo 的 `<Series>` 与
`<LocalizedSeries>`（保留英文/日文两份，见 §3 待确认）。取值优先级：

1. **元数据已有系列**（`metadata_values.Series`，人工或抓取写入）→ 直接用。
2. 否则 `path_source == ai` 且 AI 建议有 `directory` → 系列 = 该文件夹名（**不额外问模型**，零成本）。
3. 否则「自动添加系列」开 且 AI 可用 → 用模型解析系列名（新提示词 + 复用 AI 路径那条链或新作用域），
   结果写回 `metadata_values.Series`（带来源标注，可缓存、可在详情页看到）。
4. 否则（开关关 / 没有 AI）→ 不加系列，`<Series>` 回落到今天的行为（`parody or title`）。
5. 「开了但 AI 不可用」按**打包是首次还是重打**分开（运营者定，v2）：

   - **首次打包** → 任务进「需干预」并写明确错误码（如 `SERIES_AI_UNAVAILABLE`），可重试恢复；
     不静默降级 —— 与 `ai_fallback_to_rules=false` 的既有取舍一致（`app/archive/service.py:106`）。
   - **重新打包** → **先提示、再中止本次重打包**：现有 CBZ 一个字节都不动，操作者拿到一段可读原因。
     「在入队前就能判定」（总开关关 / 没配可用链）→ 直接拒绝并提示，不建任务；
     「入队后调用才发现」（供应商临时不可用）→ 中止本次打包任务、保留旧产物、把原因报到界面，
     **不写「需干预」**（重打不是必需动作，失败不该把一件已经好的书推进待办）。
     细则待你确认（见 §3 第 3 条）。

设置项：`archive_settings.auto_series`（布尔，默认关），放在**路径设置页的「AI 路径」区块**
（`app/web/templates/settings/_paths.html` 的 `ai_*` 一带），文案写明「仅 AI 可用时有效」；
`app/settings/paths.py` + `POST /archive-settings/paths/ai` 保存，`PUT /api/v1/settings/paths/ai`（R80）
如果共用同一个表单模型则一并继承。

要接的地方（打包路径是异步的，必须在 `process()` 之前解析）：

- `ConversionService` 打包前（`:1108` 附近的 `process(...)` 调用之前）算好 `series`，塞进 `_build_comicinfo`
  的参数；`_build_comicinfo`（`:1333`）把它传给 `build_comicinfo_xml(series=...)`；
  `build_comicinfo_xml`（`comicinfo.py:49`）改用它。
- 重新打包：同一条路径，自动覆盖 —— 但要在测试里显式钉一条「重新打包也走系列解析」。
- 详情页元数据抽屉：`Series` 进 `METADATA_FIELDS`，可改可锁；锁了/人工填了就走优先级 1。

### 2.4 需求 4（R84）：彻底移除「要求修订」

- 删 §1.4 表中的每一处：模型常量与状态集、`request_revision()`、API 端点（含 R70 的 `needs-revision`）、
  两个状态视图、网页路由与按钮、以及所有把它算进「待补充/未定夺」的集合（`NEEDS_INFO` 那侧只留 `NEEDS_INFO`）。
- 状态机收窄：`REVIEWABLE_STATUSES` 去掉 `NEEDS_REVISION`；`REQUEUEABLE_STATUSES` 去掉它；
  `statuses_allowing()` 相应断言更新。
- 存量数据：迁移 `025_remove_needs_revision.sql` 把 `candidates.status='NEEDS_REVISION'` 的行改成
  **`NEEDS_INFO`**（「需要补充信息」是它最接近的语义，且仍在审核面里），并顺手在
  `review_actions` 之外不动审计行。
- 审计历史（`review_actions.action='NEEDS_REVISION'`）：**一并删除**（运营者定，v3）——
  「完全移除」包括历史，时间线上这些条目随之消失。
- 破坏性：R70 的 `POST /api/v1/works/{id}/needs-revision` 消失，移动端需同步删掉这个调用 ——
  这是运营者明确要的，但要在 R84 的入口说明里点出来（契约破坏）。

---

## 3. 待确认

| # | 问题 | 我的建议 |
|---|------|---------|
| 1 | 需求 1：「标题」怎么改？ | ✅ **可**（主行改作品名、任务 ID 降副行） |
| 2 | 需求 2：每页条数的候选值？默认值改不改？ | `20 / 50 / 100 / 200`，**默认仍 50**；上限保持 200（有界扫描） |
| 3 | 需求 2：换页大小后回到第 1 页，还是保持当前页码？ | **回到第 1 页**（页码会失去意义） |
| 4 | 需求 3：「系列相关字段」 | ✅ **新开 `Series` 字段**（`Parody` 不动） |
| 5 | 需求 3：`ComicInfo` 的 `<LocalizedSeries>` 要不要也跟着系列走（英文/日文两份）？ | 跟 `<Series>` 写同一个值；`<Title>` 等其余字段一字不动 |
| 6 | 需求 3：AI 解析出的系列要不要写回 `metadata_values`（可缓存、可在详情页看到/手改），还是只写进 CBZ？ | **写回元数据**（来源标 AI），这样「重新打包」不必再问一次模型 |
| 7 | 需求 3：「仅 AI 可用时才有效」——没配 AI 时这个开关是置灰拒绝开启，还是允许保存但视为关闭？ | **置灰 + 保存时拒绝开启并说明原因**（与「页面不撒谎」一致） |
| 8 | 需求 3：「AI 不可用」怎么处理？ | ✅ **首次打包进「需干预」；重新打包提示后中止**（保留旧 CBZ） |
| 9 | 需求 4：历史审计里的 `NEEDS_REVISION` 行怎么处理？存量状态行改成哪个状态？ | ✅ **历史审计也一并删除**（v3，运营者：「完全移除」）；状态行改 **`NEEDS_INFO`** |
| 10 | 需求 1–4 是四轮分别交付（R81–R84），还是合并成一轮？ | 分开：需求 4 要迁移、需求 3 最大，分开更好审 |

---

## 4. 迁移 / 数据

- **需求 4 需要迁移 `025`**（数据迁移，不改表结构），两步：
  1. `UPDATE candidates SET status='NEEDS_INFO' WHERE status='NEEDS_REVISION'`；
  2. `DELETE FROM review_actions WHERE action='NEEDS_REVISION'`（连历史一起清）。
  这是本项目第一条「往回改数据」的迁移，需要写清升级路径与幂等（重复跑零改动）。
- 需求 2/3 不需要迁移：`page_size` 是查询参数；`auto_series` 存既有的 kv `archive_settings`；
  `Series` 是 `metadata_values` 里新出现的 `field_name`（表无字段名约束）。
- 版本号：按惯例这一轮**不提升**（仍 `v0.3.0rc3`），除非运营者另有指示。

---

## 5. 测试（要点）

- R81：`job_summary`/`_fetch_jobs` 带出与候选列表**同一个**标题；无标题候选在活动页显示 `#<gid>` 兜底；
  活动页三个 Tab 的主行都是作品名（`test_activity_web.py`）。
- R82：`?page_size=100` 真的返回 100 条；越界（0/999）被夹紧；链接保留其余查询参数；
  切换条数回到第 1 页；候选与已下载两处（`test_candidates_web.py` / `test_downloaded_web.py`）。
- R83：优先级五种分支各一条（有 Series / AI 路径文件夹 / 开+AI 解析 / 关 / 开但 AI 不可用失败）；
  ComicInfo `<Series>` 落盘断言；重新打包同样处理；锁定/人工 Series 优先；AI 不可用时任务进需干预。
- R84：`request_revision`/端点/按钮都不存在（404 或已删）；状态机断言；迁移 `025` 把存量行搬走且幂等；
  审计行仍渲染；搜索/筛选项里不再出现「要求修订」。

---

## 6. 文档同步

- `README.md`「主要能力」：活动页显示作品名、每页条数可选、系列、审核只剩通过/驳回。
- `docs/USAGE.md`：候选/已下载的每页条数；路径设置页的「自动添加系列」；审核动作删掉「要求修订」；
  打包/重新打包会写系列。
- `AgentHelp/EHBot.md`：§4.4（审核动作）、§4.5/§4.7（打包、活动页）相关小节。
- `AgentHelp/progress.md` 追加 R81–R84 条目；`PHASES.md` 四行 + 基线链；`AGENTS.md` 基线数字。
- `AgentHelp/AGENTS.md` 里若有「要求修订」的表述一并清掉。

## 7. 落地顺序（go-ahead 后）

1. R81 → 2. R82 → 3. R83（含设置项与打包路径改造）→ 4. R84（含迁移 `025` 与 API 契约删除）；
   每轮：测试 → 全量 → 文档同步 → 仅 `git add` 暂存，**不推送、不提升版本**（除非运营者指示）。
