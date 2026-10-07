# 方案：按画廊 ID 去重（摄取闸门 + 审核限制 + 一键去重）

> 状态：**已实现（R59，2026-10-07）**，本文件是落地后的设计记录。运营者「暂按这个方案进行处理」。
> v1 的「无 ID 一律不入候选」已被第 1 条答复推翻：无 ID 消息**照旧可以入候选**，限制改到审核侧；v1 的待确认 2–5 全部按建议采纳。
> 关联：`AgentHelp/AGENTS.md`（先方案后开工）、`AUTO_RULE_ACTIONS_PROPOSAL.md`（R58）、
> `CANDIDATE_ADMISSION_AND_DELETE_PROPOSAL.md`（R50，准入与彻底删除）、`progress.md` R58 条目。
> 阶段号：落地后为 **R59**；本轮**不需要数据库迁移**（见 §6）；导出词条与 v2 结论见 §10。

## 0. 需求

运营者原话（2026-10-07）：

> 必须要加上去重的内容。通过画廊ID判断作品是否已存在，已存在的不再加入候选直接忽略掉；
> 如果无法获取到画廊ID也不加入候选，也直接忽略掉；再加个一键去重的功能，移除掉重复的作品仅保留一项：
> 1. 优先移除掉未完成下载或打包的作品，优先保留已完成打包的作品；
> 2. 如果都已完成打包，保留页数较多的；
> 3. 如果都完成打包且页数一致，保留最旧的。

八点答复（逐条见 §10）把需求拆成三条可验收行为：

1. **摄取闸门**：带画廊 ID 且该 ID 已有候选的消息 → 忽略（不建候选、不并入）。
2. **审核限制**：拿不到画廊 ID 的候选**可以存在**，但**自动审批规则不得处理**（通过、驳回都不行）；
   人工审核到无 ID 候选时**给出提示**。
3. **一键去重**：对同画廊 ID 的重复候选组，每组只留一项，其余移除；保留顺序 = 已完成打包 >
   页数多 > 最旧；移除时**自动取消在途任务**，不需要人工先取消。

## 1. 现状（为什么现在会重复）

- **作品身份 = `candidates` 一行**：下载任务、归档路径、AI 路径、审核历史全挂在 `candidates.id`
  上（迁移 `015` 把这一点写成不变量）。
- 摄取时的"同一作品"是**启发式级联**而不是硬键（`app/db/database.py:1650` 起）：
  编辑同消息 → 同 `media_group_id` → 回复的目标消息 → 相邻消息（同账户/chat/发送者、±180 秒、
  `message_id` 相差 1、一条 photo 一条 archive、标题 casefold 相等）→ 画廊键
  `(ex_gid, ex_gallery_token)`；两路都命中时合并（`app/db/database.py:1733`）。
- 唯一的硬约束是建表时的 `UNIQUE (ex_gid, ex_gallery_token)`（`app/db/migrations/001_initial.sql:48`）。
  SQLite 里 NULL 互不相等，所以**无画廊候选永远不去重**；带画廊的也只在"解析出链接的那一条"上命中，
  别的消息仍可各建一行候选——重复就是从这里来的。
- 准入默认是「只解析含画廊链接的消息」（`app/candidates/parse_rules.py:40` 的
  `require_gallery_link = True`），但可用放宽开关（`accept_photo` / `accept_archive` /
  `accept_preview` / `archive_formats`）放无 ID 消息进候选；**这些开关连同相邻配对、media group
  合并、预览页候选在本方案里全部保留**（答复 1），只是它们的候选不能参与 gid 去重。
- 自动审批今天的入口：`AutomaticApprovalService.matching_rule`（`app/auto_approval/service.py:32`）
  只挡"非 PENDING_REVIEW"；`AutoApprovalSweeper.sweep_once`（`app/auto_approval/sweeper.py:139`）
  用 `pending_candidate_ids(limit=100, oldest_first=True)` 取最旧的 100 条；规则编辑器试跑
  `AutomaticApprovalService.preview` 走同一份待审列表。
- 移除设施已成熟：`ArchivedWorkService.purge_work(candidate_id, delete_files=...)`
  （`app/downloads/archived.py:399`）删候选 + 消息 + 元数据 + 审核历史 + 路径 pin + 任务 + 产物，
  写 `removed_works` 审计；下载任务在途用 `DownloadService.cancel_job`（`app/downloads/service.py:637`）
  可取消，打包中的任务由 worker 持有该行、**没有**取消接口。
- 已下载页已有批量工具栏与两种删除确认（`app/web/templates/downloaded.html:185` 起）；
  候选行与作品详情页已有 `ui.badge` / `ui.confirm` 的用法。

## 2. 设计 A：摄取闸门（只作用于带画廊 ID 的消息）

### 2.1 判定表

| 消息 | 判定 |
|---|---|
| 没有画廊 ID | **照旧走原流程**（解析规则、来源规则、AI、建候选）——不加新限制 |
| 有 ID，库中无该 ID 的候选 | 走原流程，正常建候选 |
| 有 ID，库中已有该 ID 的候选（任意状态） | `IGNORE`，理由「该画廊已有候选」；不建行、不并入、不追附件、不改元数据 |
| 编辑消息，且该消息本来就属于这个 ID 的候选 | 不算重复，走原有编辑更新路径 |
| 编辑消息，新 ID 属于别的候选 | `IGNORE`，并按现有 `deactivate_candidate_message` 解除旧关联 |

- 查重**不看状态**：待审 / 已通过 / 已驳回 / 已下载一律算"已存在"。
- 位置：`Ingestor.admit_message` / `_gate`（`app/candidates/ingestor.py:63`），**在 AI 准入之前**——
  闸门只有一次索引查询，放在最前面可以避免为一条重复消息付模型钱，也避免重复消息触发 gdata 富集。
- `IGNORE` 的现成语义就是「什么都不写」：不落 `source_messages`、不建候选，只
  `mark_telegram_update_result` 记账（`app/candidates/ingestor.py:147`），与"直接忽略掉"逐字一致。
- 查询沿 `UNIQUE (ex_gid, ex_gallery_token)` 索引的前导列，**不新增索引/迁移**。

### 2.2 竞态与最终裁决

Bot 与 MTProto 两条摄取通道并发时，闸门读与写入之间有窗口。因此在
`_ingest_message_sync` 里保留并改写 `ex_candidate_id` 分支作为**最终裁决**：
拿到同 ID 的 `ex_candidate_id`（且不等于这条消息自己的候选）时直接早退、记忽略，
不写 `candidate_messages`；`UNIQUE (ex_gid, ex_gallery_token)` 仍是最后一道防线。

`app/db/database.py:1733` 那段「启发式候选 + 画廊候选」的合并分支**保留不动**：
答复 1 让无 ID 候选继续存在，这条分支仍然可达（相邻配对产生候选后，同组里再出现带链接的消息）。

## 3. 设计 B：审核侧限制（无 ID 候选）

### 3.1 自动审批不得处理无 ID 候选

三道口子一起堵，缺一个都会漏：

1. `AutomaticApprovalService.matching_rule`：候选 `ex_gid is None` → 直接返回 `None`。
   这样自动通过、自动驳回都拿不到规则，扫描器计入"未命中"，候选留在队列里由人工处理。
2. `AutoApprovalSweeper.sweep_once` 的取数：`pending_candidate_ids` 增加
   `require_gallery: bool = False` 参数（SQL 加 `AND ex_gid IS NOT NULL`），扫描器传 `True`。
   **这不是优化，是防止饿死**：批次是最旧的 100 条 + `LIMIT`；如果队首堆着上百条永不被处理的无 ID
   候选，新候选永远进不了这一批（既有的 oldest-first 注释就是为这类窗口问题写的）。
3. `AutomaticApprovalService.preview`（规则编辑器试跑）：同样只列带 ID 的候选，
   否则试跑的「命中 N 条」与实际能落地的条数对不上。

候选页渲染时的顺带调用（`app/web/routes/candidates.py`）不用改：它会撞上第 1 条的 `None` 早退，
且该早退在读元数据之前，没有额外成本。

### 3.2 人工审核提示无 ID 候选

- **词条**：新增 `CANDIDATE_WARNING_STATUS`（`app/api/status.py`），如
  `NO_GALLERY → 「无画廊 ID」`（`TONE_WAITING`），并提供 `candidate_warning_view(candidate)`；
  模板只渲染词条，不自己写中文。
- **候选列表行**：状态徽章旁加一个「无画廊 ID」警示徽章（`app/web/templates/candidates.html`）。
- **作品详情页**：标题区/审核动作区加同一徽章与一句提示
  「没有画廊 ID：不会被自动审批规则处理，也无法参与去重」；「通过并下载」从普通按钮改为
  `ui.confirm` 确认框，确认文案把这句话再说一次（无 ID 是罕见路径，多一次确认不碍事）。
- **批量通过**：候选页当前页的无 ID 条数由服务端算出，写进批量「通过并下载」的确认文案
  （「选中的 N 件中有 M 件没有画廊 ID……」）。
- **载荷**：候选/作品序列化新增 `has_gallery`（与 `warning` 视图），移动端与 JSON 客户端
  可以用同一份事实提示；`/api/v1/candidates` 与 `/api/v1/works/{id}` 均可见。

## 4. 设计 C：一键去重

### 4.1 分组与排序

- **分组键**：`candidates.ex_gid IS NOT NULL`（答复 2：用 `ex_gid`，不要求 token），
  按 gid 分组，取成员数 ≥ 2 的组。
- **范围**：所有候选，不限状态（答复 3）——待审 / 已通过 / 已驳回 / 已下载一起参与比较。
- **保留优先级**（依次比较，全同时取 `id` 最小即最旧）：

| 优先级 | 键 | 取值 |
|---|---|---|
| 1 | 已完成打包 | 该候选有 `artifact_type = 'CBZ'` 的产物（`DownloadedWork.is_packaged` 的同一判据：产物是唯一诚实证据，`app/downloads/models.py:282`） |
| 2 | 页数 | 该 CBZ 的 `artifacts.page_count`，未打包按 -1 |
| 3 | 最旧 | `candidates.id` 最小 |

- 三条把运营者的规则 1/2/3 逐条落实，并且是**全序**，没有并列歧义。页数只有打包后才有，
  所以规则 2 实际只在"都打包"时生效——与表述一致。

### 4.2 移除（含在途任务）

- 每个败者走既有 `purge_work(candidate_id, delete_files=True, operator_name=...)`（答复 4）：
  `removed_works` 审计、路径安全校验、产物清理全部复用，不新写一套删除。
- **在途下载**：`purge_work` 会以 `WORK_STILL_RUNNING` 拒绝。去重流程在调用前，先对该候选所有处于
  开放状态的下载任务调用 `DownloadService.cancel_job(job_id)`（`app/downloads/service.py:637`），
  再 purge——这就是答复 6 的"直接移除，不需要人工取消"。
- **正在打包**：worker 持有该行，且没有取消接口（`app/conversion` 无 `CANCELLED` 状态）。
  强行删行会让 worker 事后写回产物、留下半套记录，所以这一类败者**跳过并列入摘要**
  （`skipped: [{candidate_id, code: "WORK_PACK_RUNNING", message}]`），下一次去重即可清掉
  （打包是秒级到分钟级的窗口，不需要人工操作）。这条是本方案唯一保留的"重跑一次"路径，见 §10。
- **汇总**：`{groups, kept: [...], removed: [{candidate_id, title, reason}], skipped: [...]}`；
  日志事件 `gallery_dedup_completed groups=%d removed=%d skipped=%d`。跑两次，第二次 groups = 0（幂等）。

### 4.3 界面与接口

- **已下载内容**页工具栏加「一键去重」按钮（全局动作，不依赖勾选）。
- 流程：点击 → `GET /api/v1/downloaded/dedup`（dry-run，返回每组的保留/移除/理由）→
  页面展示确认清单（答复 5）→ 确认后 `POST /api/v1/downloaded/dedup` → 回跳并显示摘要。
- 后端：`app/api/downloaded.py` 新增 `dedup_plan(...)` / `apply_dedup(...)`；
  `app/web/routes/downloaded.py` 加 GET/POST 两个路由（页面与 JSON 客户端共用 API 逻辑）。
- 鉴权 / CSRF / `operator_name` 沿用既有删除路径（`deps.validate_csrf` + `request.session["username"]`）。

## 5. 备选方案

- **只在 DB 层 ingest 去重**：更靠近写入，但两条通道 + AI 准入已经付费；否决。
- **按标题/作者指纹去重**：会误合并同名前缀不同的作品；画廊 ID 是上游给的强身份；否决。
- **迁移里自动合并存量重复**：迁移期删文件不可接受；改为一键去重显式执行；否决。
- **无 ID 候选也不准入**（v1 方案）：被答复 1 否决，已从本版移除。
- **删除 ingest 的合并分支**（v1 待确认 8）：答复 1 让它重新可达，**保留**。

## 6. 迁移

**无。** gid 查询复用 `UNIQUE (ex_gid, ex_gallery_token)` 的前导列；本轮没有列、表或索引变化。
等存量重复被一键去重清完之后，可以再单独来一波 `CREATE UNIQUE INDEX ... ON candidates(ex_gid)
WHERE ex_gid IS NOT NULL` 做硬约束（现在加会在有重复的库上直接失败）。

## 7. 测试计划

- **unit**（`tests/unit/`）：闸门判定表五行（无 ID 放行、新 ID 通过、已知 ID 忽略、编辑自身放行、
  编辑改挂忽略）；`matching_rule` 对无 ID 候选返回 `None`；`pending_candidate_ids(require_gallery=True)`
  过滤与排序。
- **integration `test_database.py`**：同一 gid 第二条消息不建候选、不挂 `candidate_messages`；
  绕过闸门直接 ingest 的竞态兜底不产生第二行；无 ID 消息仍按原路径入库。
- **integration 自动审批**：无 ID 候选即使命中规则也不动；扫描器不会在无 ID 队列前饿死
  （队首放 ≥ 批量大小的无 ID 候选，新候选仍被处理）；试跑不列无 ID。
- **integration 去重**：三条排序规则各一例 + 混合例（打包 vs 未打包、页数、同页数最旧）+
  在途下载被自动取消后再移除 + 打包中列入 skipped + 幂等（第二次 0 组）。
- **web**：无 ID 提示徽章、通过确认文案、批量确认计数；去重预览清单、确认执行、跳过项摘要。

## 8. 文档变更（交付的一部分）

- `README.md`：摄取段落加一句"同画廊 ID 不重复入候选；无 ID 候选不受自动规则处理"。
- `docs/USAGE.md`：审核一节加「无画廊 ID」提示与限制；已下载内容加「一键去重」与三条保留规则；
  解析规则放宽开关的说明补一句"放开的无 ID 候选不参与画廊去重"。
- `AgentHelp/EHBot.md`：准入去重与审核限制的要求。
- `AgentHelp/progress.md` R59 条目、`PHASES.md` 一行与基线链、`AgentHelp/AGENTS.md` 基线。

## 9. 验收标准

1. 同一画廊 ID 的消息第二次出现 → 不新增候选、不并入，`ignored` +1。
2. 无 ID 消息仍能成为候选；但它不会被自动审批规则通过或驳回，人工审核时页面上有提示。
3. 一键去重每组只留一项，保留项符合"打包完成 > 页数多 > 最旧"；在途下载先取消再移除。
4. 移除只走 `purge_work`，`removed_works` 有审计；打包中的败者按 skipped 报告，不产生半套记录。
5. 全量测试通过，文档同步。

## 10. 答复归档与未尽事项

八点答复（2026-10-07，v2 据此修订）：

| # | 运营者裁定 | 落到本方案 |
|---|---|---|
| 1 | 无 ID 的可加入候选，但在审核时限制：自动审核处理不了无 ID 的，手动审核无 ID 时提示 | §2.1 去掉"无 ID 一律忽略"；新增 §3 |
| 2 | 去重键取建议项 `ex_gid` | §4.1 |
| 3 | 范围取建议项：所有候选 | §4.1 |
| 4 | 败者删除磁盘文件 | §4.2 |
| 5 | 执行前预览确认 | §4.3 |
| 6 | 去重时直接移除，不需要再手动取消 | §4.2 自动 `cancel_job` |
| 7 | 彻底删除过的画廊再次转发：允许重新入候选 | §2.1（查重只看现有行；行没了就能再入） |
| 8 | 「什么不可达分支？」 | 指 `app/db/database.py:1733` 的"启发式候选 + 画廊候选"合并分支；因答复 1 它仍可达，**保留**，不进本方案的删除范围 |

**唯一未尽事项**：败者正在打包时，worker 持有任务行且没有取消接口，去重会把它列为 `skipped`，
需要下一次去重（或打包结束后重跑）才能清掉。这不是人工取消操作，只是重跑一次去重。
