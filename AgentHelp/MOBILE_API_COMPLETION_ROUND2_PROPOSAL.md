# MOBILE_API_COMPLETION_ROUND2_PROPOSAL — 动作面补口（下半场）

> 状态：**运营者已放行**（2026-10-11 移动端 M9 落地后，运营者对
> 「要不要接着开这批后端端点」回答「开」）。
> 上半场（删除 / 重新排队 / 手动添加 / 设置三区写入）见
> `MOBILE_API_COMPLETION_PROPOSAL.md`，已按 R64–R69 落地。

## 1. 需求

移动端 `EhBot-Mobile` 的作品详情页与已下载页仍有按钮是**禁用**状态，因为它们背后的动作
只在网页层有出口（cookie 会话 + `csrf_token` 表单字段 + 302 跳转），Bearer 客户端够不着。
运营者要求「复刻 WEBUI 的所有功能」，所以这批把它们补成 `/api/v1` 的 JSON 出口。

## 2. 现状

`app/web/deps.py::require_authenticated` 只看 `request.session`，**不接受 Bearer**；
这些 handler 都是表单 POST。要补的是六类动作 + 六个设置分区的写入：

| 能力 | 网页路由（现况） | 移动端现状 |
|------|------------------|-----------|
| 要求修订 | `POST /candidates/{id}/needs-revision` | 按钮禁用 + 说明 |
| 打包 / 重新打包 | `POST /candidates/{id}/convert` | 按钮禁用 + 说明 |
| 重获元数据 | `POST /candidates/{id}/exhentai-metadata` | 按钮禁用 + 说明 |
| 按来源拉取 | `POST /candidates/{id}/{download,telegram-user,exhentai-archive,telegraph,torrent}` | 四个来源按钮禁用 + 说明 |
| 已下载改名 / 移动 | `POST /downloaded/{id}/rename` | 未渲染 |
| 设置写入（六区） | `/sources/*`、`/settings/parse`、`/auto-approval/*`、`/archive-settings/*`、`/paths/*`、`/settings/ai/*` | 只读 + 说明 |

## 3. 设计（通则）

**复用网页那条服务调用，一条不重写。** 每个新路由都是「一次 `require_session` +
一次 `require_csrf` + 调网页 handler 调过的同一个服务方法 + `ApiError` 翻译域错误」。
这是 R64–R69 立下的规矩，也是这些端点与页面不会漂移的唯一原因。

- 全部落在 `app/api/`（动作面进 `app/api/actions.py`，设置面进 `app/api/settings.py`）。
- 需要而 `app/api/deps.py` 缺的访问器按 `app/web/deps.py` 的同名实现补。
- 成功 `publish` 与网页同样的事件；域错误带稳定 `code` + `public_message`，
  经既有 `_translate` / `raise ApiError` 原样交给客户端。
- 路由声明顺序照仓库既有规矩：字面量兄弟排在带类型参数的 catch-all 之前。

### R70 · 要求修订 + 打包 / 重新打包

    POST /api/v1/works/{candidate_id}/needs-revision
    { "reason": "…" }        # 可选，落审计

    POST /api/v1/works/{candidate_id}/convert

- `needs-revision` → `ReviewService.request_revision(candidate_id, operator, reason)`
  （网页 `needs_revision_candidate` 同款）。返回 `{candidate_id, status}`。
- `convert` → `ConversionService.enqueue_for_candidate(candidate_id)`
  （网页 `convert_candidate` 同款，一个端点同时是「打包」与「重新打包」——
  `enqueue_for_candidate` 本身按作品幂等）。返回 `{candidate_id, job_id}`。
- 迁移：无。

### R71 · 重获元数据

    POST /api/v1/works/{candidate_id}/fetch-metadata

- `ExHentaiService.fetch_metadata_for_candidate(candidate_id)`（网页同款）。
  返回 `{candidate_id, has_gallery}`。
- 未配置 ExHentai / 没有 `ex_gid` / 上游失败 → 沿用域错误码（`ExHentaiDownloadError`）。
- 迁移：无。

### R72 · 按来源拉取（五个来源）

    POST /api/v1/works/{candidate_id}/sources/{provider}
    provider ∈ { TELEGRAM, TELEGRAM_USER, EXHENTAI, TELEGRAPH, EH_TORRENT }

- 与网页 `_SOURCE_ROUTES` **同一张表**（`PROVIDER_*` → 网页路径名），API 侧复用
  `app/api/works.py::_SOURCE_ROUTES` 的键集，不另写一份。
- 分派到网页同一批调用，全部 `redownload=True`（网页即如此：再按一次就是重新拉）：
  - `TELEGRAM` → `enqueue_telegram_download(candidate_id, <第一个压缩附件>, redownload=True)`
  - `TELEGRAM_USER` → `enqueue_telegram_user_download(…同上…)`
  - `EXHENTAI` → `enqueue_exhentai_download(candidate_id, redownload=True)`
  - `TELEGRAPH` → 先 `telegraph_service` 存在性检查，再 `enqueue_telegraph_download(candidate_id, redownload=True)`
  - `EH_TORRENT` → 先 `torrent_service` 存在性检查，再 `enqueue_torrent_download(candidate_id, redownload=True)`
- 两个 Telegram 来源要先有压缩附件，否则与网页同文案拒绝（「该候选没有可下载的压缩附件」）。
- `EXHENTAI` 会花 GP：**确认是客户端的事**（网页用确认对话框，移动端同样）。
  后端不做二次拦截——运营者按下那个按钮就是决定。
- 返回 `{candidate_id, provider, job_id}`；未知 provider → 400 `PROVIDER_UNKNOWN`（`details.allowed`）。
- 迁移：无。

### R73 · 已下载改名 / 移动

    POST /api/v1/downloaded/{candidate_id}/rename
    { "filename": "…", "directory": "…", "repack": false }

- `ArchivedWorkService.rename_work(candidate_id, filename=…, directory=…)`（网页同款）；
  `repack` 为真时再 `enqueue_for_candidate`（网页的 `repack` 勾选框）。
- `filename` / `directory` 只给其中之一也合法（网页两栏都可空 = 只做移动）。
- 返回 `{candidate_id, moved, relative_path}`；`moved=false` 表示名字没变
  （客户端据此提示差异，而不是把它当失败）。
- 迁移：无。

### R74+ · 设置六个分区的写入（按区分期，每区一 commit）

    PUT /api/v1/settings/sources[/{id}]        → 增删改监听来源
    PUT /api/v1/settings/parse                 → 解析规则 + AI 候选判定开关
    PUT /api/v1/settings/auto-approval/rules[/{id}] + /dry-run
    PUT /api/v1/settings/archive/{torrent,limits,profiles}
    PUT /api/v1/settings/paths/{template,rules,ai,preview} + /rearchive
    PUT /api/v1/settings/ai/{providers,models,chain,verify}

- 每区**复用网页同一份表单校验与服务**（`ArchiveSettingsService` / `RuleFormService` /
  `AiSettingsService` / `SystemSettingsService` …），不重写校验。
- 顺序按移动端价值排：`sources` → `parse` → `auto-approval` → `archive` → `paths` → `ai`。
- 与前六期同一条铁律：**凭据永不回显**；`GET` 只给标识 / 状态 / 时间。
- 迁移：无（这些区已有表；新增的只是 JSON 出口）。

## 4. 备选（否决）

| 备选 | 否决理由 |
|------|---------|
| 让移动端直接调网页路由 | 要 cookie 会话与 `csrf_token` 表单字段、返回 302；Bearer 客户端够不着，且会污染分层 |
| 在 `app/api/` 重写一份动作逻辑 | 与网页那份必然漂移，违反仓库既有规矩（R64–R69 的同一条） |
| 用 `POST /works/{id}/sources` + body `{provider}` | 与网页的路径式来源表分裂；路径参数让「未知 provider」是一次 400 而不是一次静默 |
| 一次把六个设置区全镜像 | 审阅与回归成本过大；按区分期，每期可独立验收 |

## 5. 影响

- **页面**：无改动。新增的是 JSON 路由；网页路径、模板、跳转一字不动。
- **契约**：`/api/v1` 新增五组动作端点 + 六个设置分区写入；`openapi.json` 自动更新。
  移动端 `AgentHelp/CONTRACT.md` §7 的「仍需后端」表随之逐行清空。
- **数据库**：无迁移。
- **测试基线**：随每期递增（只跑改动触到的文件，全量留给发布前）。

## 6. 测试计划

每期只跑改动触到的文件（仓库规矩）。

- **R70**：需修订从可审状态转入 → 状态与审计都对；非法状态 → 域错误；404；convert 入队
  一个转换任务、已打包再按是重新打包（作业复用）。
- **R71**：没有 `ex_gid` 拒绝；未配置 ExHentai 拒绝；成功路径打的是同一个
  `fetch_metadata_for_candidate`。
- **R72**：五个 provider 各自打对服务方法；两个 Telegram 来源缺压缩附件时按网页文案拒绝；
  未知 provider → 400 且 `details.allowed` 是那五个；`telegraph`/`torrent` 未配置时拒绝。
- **R73**：改名 / 只移动 / `repack` 三种组合；`moved=false`；不存在的作品 404。
- **R74+**：每区一次往返（写入后 `GET` 读回）、越界值拒绝、凭据不回显。
- **回归**：网页侧对应路径的既有用例保持全绿（证明复用没改行为）。

## 7. 文档同步

- `AgentHelp/progress.md` 每期一条 R 条目；`AgentHelp/PHASES.md` 加行。
- `README.md` / `docs/USAGE.md`：新增的是内部 API 出口，部署方式不变，预计只补一句。
- 移动端仓库 `AgentHelp/CONTRACT.md` §7：每落一期就把它那一行从「仍需后端」挪到
  「已就绪」——那张表的用途就是让客户端知道哪些按钮可以解禁。

## 8. 不做（明确列出）

- **改管理员密码**（网页 `/password`）：Bearer 签发时已保证改过密码，移动端不实现。
- **UI 的二次确认 / GP 提示**：属于客户端；后端只负责「不拦」。
