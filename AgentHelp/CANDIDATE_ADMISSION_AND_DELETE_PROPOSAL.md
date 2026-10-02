# 方案：候选解析规则页、AI 候选判定、彻底删除、来源批量（R50 草案 v2）

> 状态：**已按本方案落地（R50，2026-10-01）**。实现记录见 `progress.md` 的 R50 条目，
> 阶段索引见 `PHASES.md`。落地与本文件有两处有意的偏差，写在 §9。
> v2 依据运营者 2026-10-01 的四点答复修订：AI 判定可选、解析规则独立页、
> AI 判定先于解析规则且可配置覆盖、以及“先审阅为什么删除不完善”。

## 0. 需求与设计对应

| # | 需求 | 落地 |
|---|------|------|
| 1 | 完整、正确的删除，把候选与作品彻底移除 | §2 |
| 2 | 加入候选时只解析含 Eh 链接的消息；解析规则单独设置页、可配置 | §3.3 |
| 3 | 候选加入接入 AI；AI 判定先于解析规则；可配置是否完全覆盖解析规则 | §3.1–3.2 |
| 4 | 来源规则的添加与配置支持批量操作 | §4 |
| 6 | 先审查：为什么规则配置/候选/作品没有完善删除 | §1 |

已完成的周边事项：
- `codegraph`（`@lzehrung/codegraph` 2.4.0）已安装、`codegraph install codex` 已写技能与 MCP、
  `codegraph sync --init` 已建立索引（297 文件）。
- 新增 `AgentHelp/PHASES.md`（全阶段一行索引 + `progress.md` 行号），`AGENTS.md`
  已改为“先读 PHASES.md，再开工前 `codegraph sync`”。

## 1. 审查：为什么现在没有完善的删除（需求 6）

### 1.1 规则配置：只有「来源规则」没有删除

有删除入口与实现的：自动审批规则（`delete_auto_approval_rule`）、归档路径规则
（`delete_archive_path_rule`）、归档密码（`delete_archive_password`）、AI 供应商/Key/模型
（`delete_ai_provider*`）。

**唯一缺口是 `telegram_sources`（来源）**：数据库没有 `delete_telegram_source`，页面没有删除按钮，
只有「停用」。原因不是疏忽，而是**设计上的从属关系**：

- `discover_telegram_source()` 会在**每一条来自新 chat 的消息**上 `INSERT ... ON CONFLICT DO NOTHING`
  自动建行（默认停用）。来源是“白名单条目”，不是订阅，也不是由操作者手工创建的对象。
- 因此**单纯 DELETE 会“复活”**：删掉之后，该 chat 的下一条消息又把它插回来（虽然仍是停用态）。
  这就是 R49 记录里「不做勾选后批量建来源」的真实顾虑——误点建的行只能停用、删不掉，
  而“删掉”本身在没有墓碑的情况下也不彻底。
- 结论：来源要支持“删除”，必须同时决定删除的语义（见 §4.2）：是**墓碑**（不再自动复活）
  还是**允许自动复现的移除**（等价于清空并停用）。本方案选墓碑。

### 1.2 候选：只有“驳回”，没有删除

候选行的删除在代码里**只作为副作用**存在两处：合并候选时吸收掉多余行、
编辑后最后一条消息被移除时删掉候选。`021` 迁移记录了两条路径因外键失败导致**服务起不来**，
说明当时就没有把“删候选”当一等操作设计。原因写在 `ArchivedWorkService._remove_records_sync`：

> 候选是作品的**身份**，承载元数据与审核历史，删掉会让 `review_actions` 变成孤儿。

加上 `EHBot.md §1.2.3` 的立意（“移除下载内容”不等于“这本书没出现过”），
于是候选只提供「驳回 / 重新排队」，没有删除。**缺陷在于**：
`review_actions` 与 `download_jobs` 对 `candidates` **没有 CASCADE**，
所以任何“删候选”的实现都必须显式清理子表并留审计，否则要么外键报错、要么留下悬空记录。

### 1.3 作品：`remove_work` 存在，但不完整

`remove_work` 只删 **`work.job_id`（最新下载）+ `work.pack_job_id`（打包）** 两行。
但一个候选可以有多行下载任务：每个来源一个 `idempotency_key`
（`telegram:` / `telegram-user:` / `exhentai:` / `telegraph:` / `torrent:`），最多 5 个下载行 + 1 个打包行。

**后果**：换过来源的书，移除后仍会留下兄弟来源的 `download_jobs` 行、`artifacts` 行和磁盘文件；
`/activity` 历史与 `/downloaded` 仍可能读到它们。这是现有删除**真实的不完整**，
比“没有删除按钮”更需要修。

### 1.4 结论：最优改法

1. 新增 `purge_candidate`（彻底删除）：删该候选的**全部** job 行与其 artifacts、
   `review_actions`，再删候选行（其余子表 CASCADE）；每个被删 job 写一行 `removed_works` 审计；
   文件删除仍是独立的、默认关闭的动作，且删的是**所有历史产物**而不是最新一份。
2. **顺手修 `remove_work`**：改为删该候选的**全部下载/打包 job 与 artifacts**（仍保留候选），
   使“只删记录”也不残留兄弟来源。这是行为修正，需要一条回归用例与一条 `progress.md` 说明。
3. 来源删除加**墓碑**（§4.2），让删除真正不复活。
4. 候选删除与作品删除共用 `purge`，不新增第二条“删候选”路径。

## 2. 彻底删除（需求 1）

### 2.1 数据层

- `Database.purge_candidate(candidate_id, *, deleted_files, operator_name)`：
  1. 删该候选 `artifacts`（外键指向 `download_jobs`）；
  2. 删 `download_jobs`、`review_actions`（两张表对 `candidates` 无 CASCADE，必须显式）；
  3. 删 `candidates` 行 → 级联 `candidate_messages` / `metadata_values` /
     `work_archive_paths` / `ai_path_suggestions`；
  4. 每个被删 job 写一行 `removed_works`（审计表无外键，行比候选活得久）。
- `Database.candidate_artifacts(candidate_id)`：返回**全部**历史产物 `(type, path)`，供删除文件用。
- 修 `_remove_records_sync`：按 `candidate_id` 删全部 job + artifacts（保留候选）。
- `022` 迁移：加 `idx_download_jobs_candidate`（新查询都用它）；无表结构变化，无数据迁移。

### 2.2 服务层

- `ArchivedWorkService.purge_work(candidate_id, *, delete_files=False, operator_name)`：
  复用 `remove_work` 的在途守卫（下载/打包进行中 → 拒绝）；
  开启删文件时遍历 `candidate_artifacts` + 路径钉，每个路径经 `_resolve_inside` 校验，
  失败进 `failed_files` 不阻断记录删除，空目录用 `_prune_empty_parents` 收尾。
- 删除后重定向回列表（`return_to` 经 `local_return_to`；目标是作品页本身时回落 `/candidates`）。

### 2.3 页面/接口

| 位置 | 变更 |
|------|------|
| `/works/{id}` 动作区 | +「彻底删除」「删除并删文件」（`ui.confirm` 两步；`mode=delete|delete-files`） |
| `/candidates` 批量条 | +同名两项；选中列在**全部** Tab 可用（终态 Tab 也能删） |
| `POST /candidates/{id}/delete` | 单条 purge |
| `POST /candidates/batch-review` | 兼容 `delete` / `delete-files` 两个动作 |
| `apply_candidate_delete_batch` | 逐条执行，`applied`/`skipped` 与既有批量同构 |

### 2.4 明确保留（写清楚以免被当成漏删）

- `source_messages` 原始消息：来源溯源，且删它可能让 `telegram_ingest_targets` 的派生游标回退。
- `thumbnails`：内容寻址，可能被其他候选共用。
- `removed_works`：既有“删除要留痕”约定。

## 3. 候选解析规则页 + AI 候选判定（需求 2、3）

### 3.1 准入管线（新）

```text
监听的消息
  └─ AI 候选判定（仅当：已配置 AI 供应商 且 手动开启 ai_candidate_enabled）
       ├─ 判定 reject → 不入候选（记原因）
       └─ 判定 accept
            ├─ ai_candidate_override_parse_rules = on  → 直接入候选（跳过解析规则）
            └─ off → 继续走解析规则
  └─ 解析规则（设置 → 解析规则；默认只接受含 Eh 链接的消息）
       ├─ 不通过 → 不入候选（记原因）
       └─ 通过 → 解析字段（链接/附件/预览页/标题），再走既有来源规则 → 保存候选
```

- AI 判定**先于**解析规则执行（需求 3），且**完全可选**：只有“配置了提供商 + 手动开启”才生效（需求 1 答复）。
- “完全覆盖解析规则”是独立开关：`on` 时 AI 通过即最终通过；`off` 时 AI 通过仍要过解析规则（AND）。
- AI 拒绝在所有模式下都直接拒绝（AI 先执行）。
- **顺序即代价，这是有意的**：AI 开在解析规则之前，所以每条通过消息解析器的消息都会送去模型，
  哪怕解析规则随后会把它丢掉（比如一条没有画廊链接的图片）。这是「AI 通过即覆盖解析规则」
  能成立的前提——没有这个顺序，一条无链接的消息根本没机会被 AI 放行；代价是判定开启时
  模型调用数等于解析器认得的消息数，而不是通过解析规则的消息数。要少花钱就把判定关掉。

### 3.2 AI 候选判定设置（存 `system_settings`，k/v，无迁移）

| 键 | 默认 | 说明 |
|----|------|------|
| `ai_candidate_enabled` | 关 | 生效还需已配置 AI 供应商（默认模型链非空） |
| `ai_candidate_prompt` | `DEFAULT_CANDIDATE_PROMPT` | 运营者可改；只发消息文本/来源名/链接/附件名与类型 |
| `ai_candidate_override_parse_rules` | 关 | on = AI 通过即入候选，跳过解析规则 |
| `ai_candidate_fallback` | `reject` | 模型链整体失败/未配置时：`reject`（默认）或 `accept` |

- 复用**全局默认模型链**（`CHAIN_SCOPE_DEFAULT`），不新增 scope、不改 AI 供应商页；
  `AiProviderService.complete()` 增加 `scope` 参数（默认 `archive_path` 保持现状）。
- 新增 `app/candidates/admission.py::CandidateAdmissionService`；提示词与解析在
  `app/ai/prompt.py` / 该模块内，失败写结构化日志 `ai_candidate_admission_failed`。

### 3.3 解析规则设置页（需求 2，独立页面）

- 新设置分区 **`设置 → 解析规则`**（`SETTINGS_PARSE`），与来源规则同级、单独一页。
- **默认方案**：`require_gallery_link=true`，`accept_photo=false`，`accept_archive=false`，
  `accept_preview=false` —— 即“只解析含 Eh 链接的消息”。
- 可配置项（“硬核”一点、不常改）：

| 键 | 默认 | 含义 |
|----|------|------|
| `require_gallery_link` | `true` | 必须含 ExHentai/E-Hentai 画廊链接 |
| `accept_photo` | `false` | 允许纯图片预览消息 |
| `accept_archive` | `false` | 允许压缩包附件消息 |
| `archive_formats` | `["zip","rar","7z","cbz"]` | 允许的压缩格式（`accept_archive` 开启时） |
| `accept_preview` | `false` | 允许 telegra.ph / graph.org 预览页消息 |
| `title_required` | `false` | 无标题是否直接判 `NEEDS_INFO`（默认沿用来源规则） |

- 存储：`system_settings` 的 `parse_rules_json`；服务端 `validate_parse_rules()` 严格校验
  （未知键、类型错误、格式非法都拒绝并给中文原因），页面提供「恢复默认」。
- 解析器（`CandidateIngestor._parse_message` 与 `mtproto.parse_user_message`）改为按规则判断
  “这条消息是否够格解析”，两条路径共用同一份判定。

### 3.4 摄取路径收敛

现在 Bot 路径走 `CandidateIngestor.process_pending_updates`，MTProto 路径在
`ConnectionManager._ingest_source` 里复制了一遍“解析 → 来源规则 → 保存”。
本次把这段收敛为 `CandidateIngestor` 的一个方法，两条路径都调它：
AI 门与解析规则各只有一处实现，不会漂移。

## 4. 来源批量操作（需求 4）

### 4.1 现状

添加一次一条；会话列表只能单选预填；配置是每行一个表单独提交。R49 明确记录“不做批量建来源”，
理由正是**来源没有删除入口**。所以本需求与需求 1 的“删除能力”是同一条链。

### 4.2 删除的语义：墓碑

来源会被自动发现重建，所以“删除”要做成**墓碑**：

- `022` 迁移给 `telegram_sources` 加 `dismissed INTEGER NOT NULL DEFAULT 0`（+索引）。
- 删除 = `dismissed=1, enabled=0, rules_json='{}'`；列表隐藏墓碑行；
  `discover_telegram_source()` 遇到墓碑行保持不动（不复活）；
  再次「保存来源」同一 chat 视为显式恢复（`dismissed=0`）。
- 这样“删除”是**真的删除对操作可见性**，而不是下一条消息又冒出来。

### 4.3 批量动作

| 动作 | 说明 |
|------|------|
| `enable` / `disable` | 批量启停 |
| `delete` | 批量删除（墓碑；二次确认） |
| `apply` | 把提交的过滤规则整体套用到选中来源（空字段=清空该规则，UI 写明） |
| 批量添加 | 会话列表勾选多行 → 一次建多条（统一 `enabled=0`、无规则），再用 `apply` 批量配置 |

- 数据层：`add_telegram_sources_bulk(...)`、`bulk_update_telegram_sources(ids, action, rules=None)`、
  `dismiss_telegram_sources(ids)`。
- **HTML 约束**：来源列表每行已有自己的小表单，不能再套一层批量 `<form>`。
  沿用仓库既有的 `form="<id>"` 机制：批量表单 `id="sources-batch"` 单独渲染，
  每行 checkbox 带 `form="sources-batch"` 关联过去——无嵌套表单、无 JavaScript 依赖。

### 4.4 接口

| 位置 | 变更 |
|------|------|
| `设置 → 来源规则` | 会话列表改批量添加；来源列表 +批量启停/删除/套用规则 |
| `POST /sources/batch` | 来源批量动作 |
| `POST /sources/batch-add` | 会话列表批量建来源 |
| `GET /api/v1/settings/sources` | 快照带上 `dismissed` 与解析规则设置 |

## 5. 迁移与兼容

- `022_source_dismissed_and_job_index.sql`：`telegram_sources.dismissed` + 索引；
  `idx_download_jobs_candidate`。无数据迁移。
- 设置全部走 k/v（`system_settings`），无需迁移。
- 解析规则默认值会改变默认行为（默认只收 Eh 链接消息）：**这是一次有意的行为变更**，
  需要 README/USAGE/EHBot 明确写出，并改写受影响的约 12 条摄取用例（设为宽松规则或补链接）。

## 6. 文档同步（必做）

- `README.md`：候选准入（默认只解析含 Eh 链接、AI 判定可选）、解析规则页、来源批量、彻底删除。
- `docs/USAGE.md`：同上，删除已被推翻的旧描述（如“删除只在 /downloaded”“驳回不会删除候选”需补彻底删除）。
- `AgentHelp/EHBot.md`：准入与删除的语义更新。
- `AgentHelp/PHASES.md` + `AgentHelp/progress.md`：R50 条目与基线。
- `AgentHelp/AGENTS.md`：基线链、业务不变量（如“来源删除是墓碑”“AI 判定先于解析规则”）。

## 7. 测试计划（定向，不全量）

- `tests/unit/test_archived_works.py`：purge 记录/文件/审计/在途守卫；`remove_work` 现在清全部兄弟 job。
- `tests/integration/test_candidates_web.py`、`test_work_detail_web.py`：删除入口、两动作、确认。
- `tests/integration/test_candidate_ingestion.py`：解析规则默认/宽松；AI 判定顺序与覆盖。
- `tests/unit/test_ai_candidates.py`（新）：提示词、判定解析、失败兜底、`scope=default`。
- `tests/integration/test_settings_web.py`：批量添加/启停/删除/套用；表单无嵌套（`markup.py`）。
- `tests/integration/test_database.py`：022 迁移与墓碑语义。
- 交付说明列实际跑过的文件与实际 `collected`。

## 8. 风险与取舍

- **默认行为变更**：解析规则默认只收含 Eh 链接的消息，会让纯图片/纯压缩包/纯预览页来源静默失效；
  这是需求 2 的原文，但必须让操作者知道“去解析规则页可放宽”。
- AI 判定默认关闭，且必须“已配置供应商 + 手动开启”才生效；开启而模型链不可用时默认 `reject`，
  页面与日志都会说明原因。
- 彻底删除不可恢复（尤其 `delete-files`），沿用破坏性动作两次确认。
- `remove_work` 的行为修正会改变“移除记录”后仍残留兄弟来源 job 的旧表现，属修缺陷。

## 9. 落地时与本方案的两处偏差

- **`GET /api/v1/settings/sources` 不再回传 `dismissed`。** 墓碑行本来就不进 `list_telegram_sources`
  的结果，页面与接口都看不到它们，没有读者需要一个恒为 0 的字段；留一个「这里可能有隐藏行」的
  字段反而会让人以为该过滤它。
- **批量添加会顺手恢复墓碑。** §4.2 说「再次保存同一 chat 视为显式恢复」——批量添加被会话列表
  勾中，与单独保存是同一个意图，所以 `add_telegram_sources_bulk` 在 `ON CONFLICT DO NOTHING`
  之后对选中的 chat 执行 `dismissed=0`（只恢复可见性，不动既有规则与启用状态）。否则运营者
  删掉一个来源、又在会话列表里看到它并勾选添加，会得到「计入已有、却依然不出现」的死角。
