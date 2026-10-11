# MOBILE_API_COMPLETION_PROPOSAL — 让移动端能用上与网页等价的动作面

> 状态：**草稿，待运营者审阅**（`AgentHelp/AGENTS.md`「先寫方案後開工，審閱之後再動手」）。
> 2026-10-11 运营者在移动端实测后反馈「删除 / 下载方式切换 / 重新下载没有实现」。
> 核查结论：**大部分已经实现**，真正缺的是四类动作在 `/api/v1` 上没有出口（§2）。
> 运营者定调：「可以补上，不过要遵循原仓库的规范」，且行为与 WEBUI 一致。

## 1. 需求

移动端（独立仓库 `EhBot-Mobile`）需要在手机上调到与网页等价的动作。核查后确认，
后端已有：下载方式切换、重新下载、重新打包、移除 / 移除并删文件、任务五动作、优先级。
**缺的是**：

1. 候选**彻底删除 / 彻底删除并删文件**
2. 候选**重新排队**
3. **手动添加**
4. **设置写入**

## 2. 现状

### 2.1 `/api/v1` 已有（Bearer 可直接调，`require_session` 接受 Bearer、`require_csrf` 对 Bearer 豁免）

| 能力 | 端点 |
|------|------|
| 审核 | `POST /candidates/batch`（`REVIEW_BATCH_ACTIONS`） |
| 任务生命周期 | `POST /jobs/{job_id}/{action}`、`POST /jobs/batch` |
| 优先级 | `POST /jobs/{job_id}/priority` |
| 下载方式切换 | `POST /jobs/{job_id}/switch-source` |
| 重新下载 / 重新打包 / 移除 | `POST /downloaded/batch`（`DOWNLOADED_BATCH_ACTIONS`） |
| 去重 | `GET`/`POST /downloaded/dedup` |
| 元数据编辑 | `PATCH /works/{candidate_id}/metadata` |
| 日志 | `GET /logs`、`GET /logs/stream` |
| 设置读 | `GET /settings`、`GET /settings/{section}` |

### 2.2 `/api/v1` 缺的（只在网页层）

| 能力 | 网页路由 | 说明 |
|------|----------|------|
| 候选彻底删除 / 删文件 | `POST /candidates/{id}/delete`、`POST /candidates/batch-review` | `CANDIDATE_DELETE_ACTIONS` |
| 候选重新排队 | `POST /candidates/{id}/requeue` | `ReviewService.requeue_candidate` |
| 手动添加 | `GET`/`POST /manual-add` | R63 语义 |
| 设置写入 | `/settings/*`、`/archive-settings/*`、`/sources/*`、`/settings/ai/*` | 共十几个端点 |
| 重获元数据 / 四个来源拉取 / 转换 / 需修订 / 改名 | `/candidates/{id}/*`、`/downloaded/{id}/rename` | 本期不做，见 §6 |

网页层够不着的原因：`app/web/deps.py::require_authenticated` 只读 `request.session`，
**不接受 Bearer**；且这些 handler 是表单 POST（`csrf_token` 是必填 Form 字段、成功返回 302）。

## 3. 设计

### 3.1 通则

- **复用同一服务对象与同一条代码路径**，不写第二份逻辑——沿用 `app/api/actions.py`
  开头立下的规矩：「Review orchestration is delegated to `ReviewOrchestrator`, the same
  object the HTML routes use. That shared path is what guarantees the API cannot approve
  a candidate the page would have refused.」
- JSON 路由入 `app/api/`；错误走 `ApiError` + 既有 `_translate`；成功 `_publish` 事件。
- 遵守 Router include order：带类型参数的路由排在字面量兄弟之后。

### 3.2 候选彻底删除

**扩展现有端点**，不新增 URL：

    POST /api/v1/candidates/batch
    { "action": "delete" | "delete-files", "candidate_ids": [12, 13] }

- `api_batch_review` 改为按集合分派：`action ∈ REVIEW_BATCH_ACTIONS` → `apply_review_batch`；
  `action ∈ CANDIDATE_DELETE_ACTIONS` → `apply_candidate_delete_batch`；
  两者都不在 → 400 `ACTION_UNKNOWN`（`details.allowed` 给并集）。
- 与网页 `batch-review` 同构，返回沿用 `{action, requested, applied, skipped}`。
- 两个动作是两个词（`delete` / `delete-files`），照搬 R50 的理由：误发的那个不能是连文件一起删的那个。

### 3.3 候选重新排队

    POST /api/v1/works/{candidate_id}/requeue
    { "note": "…" }        # 可选

- 复用 `ReviewService.requeue_candidate(candidate_id, operator, note)`（网页同款）。
- 需在 `app/api/deps.py` 增 `review_service` 访问器，对齐 `app/web/deps.py` 的同名访问器
  （目前 API 侧只有 `review_orchestrator`）。
- 域错误经 `_translate`；`CANDIDATE_NOT_FOUND` → 404；成功 `_publish(EVENT_CANDIDATE)`。

### 3.4 手动添加

    POST /api/v1/candidates/manual
    { "url": "https://exhentai.org/g/<gid>/<token>/" }

- **先把 `app/web/routes/manual_add.py::_ingest_manual_link` 抽成共享服务**
  （建议 `app/candidates/manual.py`），网页与 JSON 两处共用，避免第二份实现漂移。
  网页的路由签名、模板与跳转一字不动。
- 语义严格照 R63：**形状 → 判重 → 真拉一次画廊 → 建候选**。
  - 成功 → 200 `{ "candidate_id": N }`（网页是 303 到 `/works/N`，JSON 不跳转）
  - 未配置 ExHentai → 400 `EXHENTAI_NOT_CONFIGURED`（文案同 `NOT_CONFIGURED_MESSAGE`）
  - 形状不合法 / 画廊读不到 → 400
  - 重复 → 409 `CANDIDATE_DUPLICATE` + `existing_candidate_id`
  - 磁力链接 → 400（R63 已不再接受）

### 3.5 设置写入（按区分期）

    PUT /api/v1/settings/{section}

- **先做移动端最需要的三区**：
  - `connections`：Telegram Bot Token / ExHentai 三项 cookie、断开
  - `system`：来源并发、轮询间隔、自动审批间隔、时区、日志等级
  - `passwords`：归档密码新增 / 删除；API Key 生成 / 撤销
- 每区**复用网页同一份校验与写入服务**（`ArchiveSettingsService` / `SystemSettingsService` /
  `ConnectionManager`），不重写校验。
- 凭据永不回显：`GET` 只给标识 / 状态 / 时间（现有 `GET /settings/{section}` 已经如此）。
- 其余六区（`sources` / `parse` / `auto-approval` / `archive` / `paths` / `ai`）留后续期，
  每区一 commit。

## 4. 备选（否决）

| 备选 | 否决理由 |
|------|---------|
| 让移动端直接调网页层路由 | 网页层要 cookie 会话 + `csrf_token` 表单字段、返回 302；Bearer 客户端够不着，且会污染「页面层不做 JSON」的分层 |
| 在 `app/api/` 重写一份删除 / 排队逻辑 | 与网页那份必然漂移，违反仓库既有规矩 |
| 九个设置区一次全镜像 | 审阅与回归成本过大；按区分期，每期可独立验收 |
| 用 `DELETE /api/v1/works/{id}?files=1` | 与网页的批量语义分裂；`delete` / `delete-files` 两个词的区分会丢在 query 里 |

## 5. 影响

- **页面**：无改动。新增的是 JSON 路由；网页路径、模板、跳转一字不动。
- **契约**：`/api/v1` 新增 3 组端点（删除扩展现有、requeue 新增、manual 新增）+ 设置写入分期；
  `openapi.json` 自动更新。
- **数据库**：无迁移。
- **测试基线**：1871 collected / 0 failed → 按新增用例递增（预计 +30 上下，分四次 commit）。

## 6. 本期不做（明确列出，免得读成遗漏）

重获元数据（`exhentai-metadata`）、四个来源拉取（`torrent` / `telegraph` /
`exhentai-archive` / `telegram-user`）、转换 / 重新转换、需修订、已下载改名
——仍在网页层，移动端若要，另开一期。

## 7. 测试计划

只跑改动触到的文件（仓库规矩：全量留给运营者要求或发布前）。

- **删除**：单条与批量、`delete` 与 `delete-files` 的文件取舍、在途下载 / 打包时的拒绝、
  不存在的候选 → 404、`skipped` 语义、Bearer 免 CSRF 而 cookie 会话仍需 CSRF。
- **重新排队**：从 `FAILED` / `REJECTED` 回队列、不可重排状态给出域错误、404、`note` 落审计。
- **手动添加**：正常建候选、重复 409 带 `existing_candidate_id`、未配置 ExHentai 400、
  形状非法 400、磁力 400、失败不建候选。
- **设置写入**：三区各往返一次（写入后 `GET` 读回）、越界值拒绝、凭据不回显。
- 回归：网页侧对应路径的既有用例必须保持全绿（证明抽服务没改行为）。

## 8. 文档同步

- `AgentHelp/progress.md` 追加 R 条目（每期一条）；`AgentHelp/PHASES.md` 加行。
- `README.md` / `docs/USAGE.md`：涉及对外能力描述时补一句（新增的是内部 API 出口，
  部署方式不变，预计改动很小）。
- `AgentHelp/MOBILE_CLIENT_PROPOSAL.md`：补一节「B1/B2 之后的移动端 API 补口」，
  指向本文件，并更正「只剩 B1/B2 未做」的旧表述。

## 9. 需要运营者拍板

1. 设置写入**按区分期**（先 `connections` / `system` / `passwords` 三区）可以吗？
2. §6 那批（重获元数据 / 四个来源拉取 / 转换 / 改名）要不要一并排进来？
3. 手动添加要把 `_ingest_manual_link` 从 `manual_add.py` 抽成共享服务（会动到该文件），接受吗？
4. 删除沿用「扩展 `candidates/batch`」而不是新开 `DELETE /works/{id}`，认可吗？
