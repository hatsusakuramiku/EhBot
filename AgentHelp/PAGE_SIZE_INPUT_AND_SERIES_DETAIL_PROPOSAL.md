# 方案：每页条数无上限 + 详情页可读写归档元数据（R85 候选 v3，2026-10-12）

**状态：待审阅 v3。** 只做了调研，未改业务代码。

- v1 → v2：上限 1000 作废（**无上限**）；详情页改为列出 CBZ 里对应的元数据；
  编辑后同步修改归档文件；支持手动添加字段。
- v2 → v3（运营者 2026-10-12 答复）：**只改元数据、不移动/重命名文件**；归档分区
  **可读可写**（不是只读展示）；归档元数据**存进数据库**；**移动端可读可修改**。

关联：`AgentHelp/AGENTS.md`（先方案后开工；文档是改动的一部分）、
`ACTIVITY_PAGING_SERIES_REVISION_PROPOSAL.md`（R82 / R83）、`AgentHelp/progress.md` R73（重命名）。
阶段号：建议 **R85**。**无数据库迁移**。

---

## 0.1 已定结论

| # | 问题 | 结论 |
|---|------|------|
| 1 | 改元数据是否连带重命名/移动已打包文件 | **不**。只把元数据写进文件和数据库；改名用「重新打包」或 R73 的重命名 |
| 2 | 归档元数据分区只读还是可写 | **可读可写**，直接编辑该分区即可 |
| 3 | 归档元数据要不要进数据库 | **要**。每个元素在 `metadata_values` 里有同名字段（没有就新建，来源＝人工） |
| 4 | 移动端 | **可读可写**：`GET /api/v1/works/{id}` 读出、`PATCH /api/v1/works/{id}/metadata` 写入 |
| 5 | 顺序 | 先出文档（本文件），确认后开工 |

---

## 1. 现状证据

### 1.1 每页条数是「固定档位 + 静默截断」

`app/api/contracts.py:26` `MAX_PAGE_SIZE = 200`、`:33` `PAGE_SIZE_OPTIONS = (20, 50, 100, 200)`；
`PageParams.from_values`（`:112-121`）`min(int(page_size), max_page_size)`——超了**悄悄**变 200。
页面只有四个档位、**没有输入框**（`app/web/templates/components/ui.html:193-203`）。

### 1.2 元数据面板只渲染「库里已经有行」的字段，且对文件一无所知

`app/db/database.py:4011` `SELECT ... FROM metadata_values WHERE candidate_id = ?` →
`app/api/works.py:514` → `work_detail.html:192` 遍历。所以 `Series` 为空的作品**没有那一行**，
看不到也填不了；面板里也没有任何「文件里现在是什么」的信息。

### 1.3 表里本来就存得下「额外字段」

`metadata_values` 的写入是插入式 upsert（`app/db/database.py:3293`），字段名不受限：
抓取回来的 `Web` / `FileSize` 就**不在** `METADATA_FIELDS` 里，照样入库
（`app/exhentai/gdata.py:292-298`，`app/review/models.py:130-131` 只有标签）。
限制只来自两处人工入口：`app/review/service.py:82` 与 `app/api/actions.py:686`。
→ 需求 4（手动加字段）的真正成本只是**放宽这两处校验 + 加 UI**。

### 1.4 现在「改元数据」是入队一次完整重新打包

`app/web/routes/candidates.py:669-678` 与 `app/api/actions.py:717-727`：保存成功后
`enqueue_for_candidate()` → 重新解压原档、重新压 CBZ（`app/conversion/service.py:1148-1166`）；
原档被删时直接 `ARCHIVE_MISSING`。几百 MB 只为改一行标题，这正是需求 3 要换掉的东西。

### 1.5 CBZ 里的元数据 = `ComicInfo.xml`，一处生成

`app/conversion/comicinfo.py:32 build_comicinfo_xml()` 的映射：

| 元数据字段 | ComicInfo 元素 |
|---|---|
| `Title` | `<Title>` |
| `Series`（空则回退 `Parody` → `Title`） | `<Series>` |
| `Series` / `JapaneseTitle`（空则回退 `JapaneseTitle` → `Title`） | `<LocalizedSeries>` |
| `Group`（空写 `EhBot`） | `<Publisher>` |
| `Artist` | `<Writer>` / `<Penciller>` / `<CoverArtist>` |
| `Character` | `<Characters>` |
| `Language`（ISO 639-1） | `<LanguageISO>` |
| `Category` | `<Genre>` |
| `Tags`（逗号连接） | `<Tags>` |
| `Rating`（两位小数） | `<Rating>` |
| `Description` | `<Summary>` |
| `Pages` | `<PageCount>`（写**实际页数**） |
| `Web` | `<Web>` |
| （画质设置） | `<ScanInformation>` |
| （常量/生成） | `<Manga>Yes`、`<Added>打包时刻` |

`Uploader` 等**不进**文件。这就是需求 2 说的「CBZ 里对应的元数据」的完整清单。

---

## 2. 设计

### 2.1 每页条数：无上限、可手填

- `app/api/contracts.py`：删除 `MAX_PAGE_SIZE` 与截断分支；`PAGE_SIZE_OPTIONS` 降级为**快捷档**。
  `from_values` 只做「非法不采纳」：非整数 / 空 / `< 1` → `DEFAULT_PAGE_SIZE`（50）；其余原样使用。
- `ui.pagination`：快捷档旁加 GET 表单 `每页 [ 50 ] 条 [确定]`，hidden 里带全当前查询参数
  （`q` / `sort` / `direction` / facets…），**丢掉 `page`**（换条数回第 1 页）。
  `<form method="get">`，关掉 JS 也能用，URL 仍是唯一状态源。
- `app/web/deps.py`：新增 `page_size_form_fields(request)` 与 `page_size_input_value(page)`。
- JSON API 同步放开（`limit` 不再截断）。条数**不落库**，仍是 URL 参数。

### 2.2 详情页「归档文件（ComicInfo.xml）」分区：可读可写

- 位置：元数据面板下方新增分区；**数据来自真的读 CBZ**（`zipfile.read("ComicInfo.xml")`），
  不是拿数据库反推。
- 每行 = 一个 ComicInfo 元素：`元素名 | 值（输入框，可编辑）| 保存`，
  旁边小字显示**文件中的当前值**（与输入框不一致时高亮「文件中的旧值」）。
- 保存行为 = **一次写两处**：`metadata_values`（`field_name` = 元素名，来源 `OPERATOR`）
  → 再同步进 CBZ。两条路径都走同一个服务方法，页面与移动端因此完全一致。
- 只读行：`<PageCount>`（取文件/产物的实际页数）、`<Manga>`、`<Added>`（生成值），标注不可编辑。
- 未打包：分区显示「尚未打包」，并按 1.5 的映射把**将来会写入**的字段列出来（值取自当前元数据）。
- 输入框里没有值的元素（例如文件里没有 `<Translator>`）也列出来？——**不列**：
  分区只列「文件里真实存在的元素 + 1.5 表里可写的已知元素」，其余靠「添加字段」（§2.4）。

### 2.3 编辑元数据 → 就地重写归档文件（替换「入队重新打包」）

- 新能力 `app/archive/backends/zip_backend.py::rewrite_comicinfo(path, comicinfo)`：
  重建 zip（先写新的 `ComicInfo.xml`，再按原顺序流式复制其余成员，`ZIP_STORED` 与打包一致），
  写临时文件 + `os.replace` **原子替换**。不重新解压/解码任何图片，也不依赖原始压缩包还在。
- 新方法 `app/downloads/archived.py::sync_archive_metadata(candidate_id) -> dict`：
  - 未打包 → `{"synced": False, "reason": "WORK_NOT_PACKED"}`（DB 已改，下次打包生效）；
  - `pack_state == RUNNING` → 不同步，`WORK_PACK_RUNNING`，页面/接口给提示；
  - 候选状态命中 `_PACK_FORBIDDEN_STATUSES`（驳回 / 待审核 / 待补充）→ 不同步，与打包门禁一致；
  - 否则 → 用**当前元数据**重建 ComicInfo、原子替换、重算 `sha256` / `size_bytes` 写回
    `artifacts` 行（形状同 `ConversionService._record_cbz_artifact_sync`，但不动 `job_id` / `page_count`）。
- 复用构建逻辑：把 `ConversionService._build_comicinfo`（`:1475`）提到
  `app/conversion/comicinfo.py::build_from_metadata(metadata, title, page_count, image_quality, series)`，
  打包与同步共用一份映射，杜绝两处漂移。
- 调用点改造：
  - `app/web/routes/candidates.py:648 edit_metadata`：把「入队重新打包」换成同步，页面回一句结果；
  - `app/api/actions.py:648 patch_metadata`：同上（见 §2.5）。
- 审计：沿用 `EDIT_METADATA`，`details` 加 `{"archive_synced": true, "path": ...}`，不新增动词。
- **不移动、不重命名**文件（结论 1）：路径/文件名照旧，只有文件内容与数据库变。

### 2.4 手动添加元数据字段

- 页面：元数据面板底部 `[字段名] [值] [添加]`，POST 到同一个 `POST /candidates/{id}/metadata`。
- 校验放宽（`app/review/service.py:75` + `app/api/actions.py:686` 两处共用一条规则）：
  允许 `METADATA_FIELDS` ∪ 自定义名 `^[A-Za-z][A-Za-z0-9_]{0,63}$`；
  黑名单：`RAW_METADATA_FIELDS`、`TAG`、`FileSize`、`Pages`（派生或伪字段）。
- 是否写进文件：命中小节 1.5 的映射或 ComicInfo 元素白名单
  （`Translator` / `Editor` / `Year` / `Volume` / `Number` / `Count` / `AlternateSeries` /
  `SeriesGroup` / `StoryArc` / `Notes` / `AgeRating` …）就写进 XML；
  其余只存数据库，页面在那一行标「仅数据库，不写入归档」。
- 删除字段：**本次不做**（只加/改；删除会牵动审计与文件清理语义，另开一条）。

### 2.5 移动端（可读可写）

- 读：`GET /api/v1/works/{candidate_id}`（`app/api/works.py:563`）新增
  `archive_metadata`：
  ```json
  {"packaged": true, "path": "/library/…/book.cbz", "synced_at": "…",
   "elements": [{"name": "Series", "field_value": "…", "file_value": "…",
                 "in_file": true, "editable": true, "derived": false}]}
  ```
- 写：`PATCH /api/v1/works/{candidate_id}/metadata`（`app/api/actions.py:648`）放宽自定义字段名，
  并把「入队重新打包」换成就地同步。
  **契约破坏**：响应里 `repacked` 改为 `archive_synced`
  （`{"archive_synced": true, "sync_skipped": null}`），移动端要跟着改；
  显式重新打包仍走 `POST /works/{id}/convert`。写进 `docs/USAGE.md`。
- 网页与 JSON 两条路径都调用同一批服务方法（`set_manual_metadata` → `sync_archive_metadata`），
  与仓库既有做法一致（页面能做、JSON 也能做，且判定只有一份）。

---

## 3. 不做的事

- 不自动重命名/移动已打包文件（结论 1）。
- 不做删除自定义字段（只加/改）。
- 不把 `PageCount` / `Manga` / `Added` 做成可编辑（它们是产物事实与生成值）。
- 不新增设置项：每页条数仍是 URL 参数，归档同步不需要开关。

---

## 4. 测试与文档

- 分页：`test_candidates_web.py` / `test_downloaded_web.py`（手填生效、非法回落 50、换条数回第 1 页）；
  删掉既有 `MAX_PAGE_SIZE` 截断断言。
- ComicInfo：`tests/unit/test_comicinfo.py`（映射表、`build_from_metadata`、自定义元素白名单）。
- 归档分区：`test_work_detail_web.py`（渲染文件中的值、未打包文案、值不一致高亮、只读行）。
- 同步：`test_archive_workflow.py`（改元数据 → CBZ 内 `ComicInfo.xml` 真变、`artifacts` 摘要更新、
  正在打包时跳过、未打包不动文件、驳回状态不同步）。
- 字段：自定义字段新增、仅数据库分支、非法名拒绝。
- 移动端：`test_api_domains.py`（`archive_metadata` 读、`PATCH` 写后文件同步、`repacked`→`archive_synced`）。
- 文档：`README.md`、`docs/USAGE.md`（每页条数无上限；详情页归档分区可读写；
  改元数据就地同步不再重打；自定义字段；移动端契约变化）、`AgentHelp/progress.md` 追加 R85、
  `PHASES.md` 加行、`AGENTS.md` 基线更新。无迁移，`schema_migrations` 仍 25 条。

---

## 5. 验收

1. 候选/已下载页脚可手填**任意正整数**条数（300、1000…）并立即生效；0 或乱码回落 50。
2. 详情页归档分区显示**文件里真实的值**；改一行保存 → 数据库与 CBZ 同时变，文件名/路径**不动**。
3. 未打包时分区说明将来会写什么；正在打包时给提示且**不破坏文件**。
4. 能手工加字段（如 `Translator`）并存进数据库；`Translator` 出现在 ComicInfo 里，
   非 ComicInfo 名字的字段只存库并标注。
5. 移动端：`GET /api/v1/works/{id}` 看得到 `archive_metadata`；
   `PATCH .../metadata` 改 `Series` 后 CBZ 里的 `<Series>` 同步变化。
