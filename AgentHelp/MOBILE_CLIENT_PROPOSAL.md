# 方案（第二稿）：Flutter 移动端（目标：基本可用）+ 服务端密码 / API Key 登录

> 状态：**已实施（R53 = B1、R54 = B2，2026-10-03）**。服务端鉴权已完成（迁移 `023`、
> `/api/v1/auth/*`、统一 Bearer 守卫、密码库单 Key 面板、令牌有效期）；M0–M4 客户端在
> `/home/coder/workspace/ehbot-mobile` 由另一会话实施（该仓库已按 §8 建好文档骨架）。
> 本文件保留为设计记录。
>
> 需求来源（运营者原话）：
>
> 1. 另起一个仓库做 Flutter 移动端；仓库根目录在当前仓库根目录的**上一级**
>    （`/home/coder/workspace/ehbot-mobile`，与 `EhBot` 平级）；
> 2. 客户端支持配置服务器连接；
> 3. 支持两种登录：**密码登录**或 **API Key 登录**；
> 4. 先分析需要改动的地方，出方案再审。

## 0. 审阅反馈与本稿结论

| 项 | 你的决定 | 本稿落地 |
|----|----------|----------|
| 客户端权限 | **完整权限** | Bearer 通道等同管理员，不设只读 scope（§4.1） |
| API Key 有效期 | **不过期** | `expires_at = NULL`，无自动过期 |
| API Key 生成 | **默认不生成，设置里手动生成/刷新** | 全新部署无 Key；密码库面板按钮生成；刷新=换新并使旧 Key 立即失效 |
| API Key 数量 | **任意时间最多一条有效** | 部分唯一索引 `WHERE kind='api_key' AND revoked_at IS NULL` |
| 改密码 | **不刷新 KEY** | 吊销密码认证派生的 access/refresh；API Key 不受影响（§4.6） |
| 刷新/撤销 Key | 只影响 Key 认证的会话 | Key 认证与密码认证两族完全隔离（§4.6） |
| 缩略图 | 要**本项目最合适**的方案 | 结论：客户端带鉴权的图片加载器，后端零改动（§5） |
| 交付目标 | **规划到基本可用状态** | 定义 MVP 验收线 + 分期（§7） |
| 新仓库文档 | **复刻本仓库的 agent 文档管理方式与结构** | 结构、规则、差异（含 codegraph 不支持 Dart）见 §8 |

**缩略图是唯一需要你再点头的结论项**（§5、§13）。

---

## 1. 需求与落点

| # | 需求 | 落点 |
|---|------|------|
| 1 | 移动端单开仓库 | 新仓库 `../ehbot-mobile`；本仓库只提供 HTTP 契约，不引入 Flutter 目录 |
| 2 | 客户端可配服务器连接 | Flutter「服务器」设置页：base URL（含 `APP_ROOT_PATH` 前缀）、TLS/自签策略、超时、连通测试 |
| 3 | 密码登录 | `POST /api/v1/auth/login`，复用 `admin_users` 与现有失败锁定；签发 access + refresh |
| 4 | API Key 登录 | 迁移 `023` 建凭据表；网页「设置 › 密码库」单 Key 管理；移动端 `Authorization: Bearer` |
| 5 | 完整权限 | 统一 `app/api/deps.py` 鉴权入口，Bearer 与浏览器会话同权 |
| 6 | 基本可用 | §7 的 MVP 验收线 + 分期，后端两期 + 移动端五期 |

---

## 2. 仓库拆分

### 2.1 现状

父目录 `/home/coder/workspace` 当前只有 `EhBot`。本仓库根部 `AGENTS.md` 是指针，
规则在 `AgentHelp/AGENTS.md`；`codegraph` 索引是仓库本地的 `.codegraph/`。

### 2.2 决策：`/home/coder/workspace/ehbot-mobile`

- **依赖方向单向**：`ehbot-mobile → EhBot` 的 HTTP 契约；后端不 import 移动端，
  移动端不复制后端业务代码。
- **工具链/发布解耦**：Dart/Flutter + Android/iOS 构建 vs Python/Docker `latest` 镜像。
- **不污染后端基线**：本仓库测试基线（R52 = `1690 collected`）、`codegraph sync`、
  `git log` 保持干净；新仓库有自己的基线。
- **契约是唯一桥**：`openapi.json` 快照 + `{error:{code,message,details}}` /
  列表信封（`app/api/contracts.py`）+ 稳定错误码。

### 2.3 备选（否决）

- 当前仓库加 `mobile/` 子目录：Gradle/Podfile/pubspec 污染 Python 仓库与索引。否决。
- monorepo：要重排现有仓库与全部 R 编号/部署文档，收益只是「一个 clone」。否决。

---

## 3. 现状盘点（证据）

**浏览器会话是唯一的鉴权方式：**

- `app/main.py:112` 装 `SessionMiddleware`（`same_site="lax"`，`https_only` 由
  `SESSION_COOKIE_SECURE` 决定）；会话密钥持久化在 `<data>/private/session_secret_key`。
- `app/web/routes/auth.py:80` `POST /login`：表单 `password` + `csrf_token`
  （`:92` 校验），成功后写 `authenticated` / `username` / `csrf_token` /
  `must_change_password`（`:139-143`）。
- 管理员凭据在 `admin_users`（迁移 `002`），读写 `Database.get_admin_auth` /
  `set_bootstrap_admin` / `change_admin_password`（`app/db/database.py:1079` 起）。
- 首次启动的一次性密码写 `<data>/bootstrap_admin_password`，改密后删除。

**JSON 层守卫（为浏览器 `fetch` 设计）：**

- `app/api/deps.py:26` `require_session`：401 `NOT_AUTHENTICATED` /
  403 `PASSWORD_CHANGE_REQUIRED`，故意不重定向。
- `app/api/deps.py:46` `require_csrf`：只认请求头 `X-CSRF-Token`，常数时间比较。
- 全仓 **24 处** 调用，顺序固定；前端 CSRF 来自 `base.html:31` 的 meta，
  由 `candidates.js:312` 等注入请求头。

**失败锁定**：`app/web/routes/auth.py:47-57`，`MAX_FAILED_ATTEMPTS=5`、
`LOCKOUT_SECONDS=60.0`、`MAX_TRACKED_CLIENTS=1024`；计数在 `app.state.login_attempts`，
键是 `request.client.host`；锁定时记 `LOGIN_LOCKED_OUT` 并返回 429。

**私密文件模式**：`app/secrets.py` 的 `SecretStore`（`^[a-z0-9_]+$`，`<data>/private/`，
权限收紧）；归档密码与 AI 密钥都走这条路径，页面从不回显明文。

**迁移**：`app/db/database.py:1040` 按文件名顺序执行 `app/db/migrations/*.sql`，
版本记在 `schema_migrations`；最新 `022`。

**缩略图**：`app/api/thumbnails.py`，见 §5。

**结论**：原生端没有可用入口；今天只能自己维护 cookie jar + CSRF，脆弱且跨平台不一致。
必须新开 `Authorization` 通道，同时保持浏览器路径字节不变。

---

## 4. 鉴权设计

### 4.1 凭据形态：统一 `Authorization: Bearer`

格式统一为 `<public_id>.<secret>`：

| 类型 | 前缀 | 生命周期 | 来源 | 数量 |
|------|------|----------|------|------|
| API Key | `ehk_` | **不过期**，可吊销 | 网页「设置 › 密码库」手动生成 | **全局最多 1 条有效** |
| Access token | `eha_` | 短期（默认 12h） | 密码登录签发 | 每设备一套 |
| Refresh token | `ehr_` | 长期（默认 30d） | 密码登录签发，刷新时轮换 | 每设备一套 |

- **完整权限**：Bearer 凭据与浏览器管理员会话同权——`require_session` 通过即视为
  `authenticated`，`must_change_password` 检查在签发时已保证，不再设只读 scope。
- `public_id`：随机短串（16 字符）用于常数时间查找；`secret`：32 字节高熵随机串。
- 只存 `sha256(secret)`：高熵随机串下 sha256 足够防库泄露，且每请求一次哈希，
  不需要 argon2 的成本与随机盐。比较用 `hmac.compare_digest`。
- `last_used_at` 更新**节流**（距上次写 > 60s 才写），避免每请求一次 DB 写。

### 4.2 数据模型（迁移 `023_api_credentials.sql`）

```sql
CREATE TABLE api_credentials (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL CHECK (kind IN ('api_key','access','refresh')),
  label         TEXT NOT NULL DEFAULT '',
  public_id     TEXT NOT NULL UNIQUE,
  secret_hash   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  expires_at    TEXT,          -- NULL = 不过期（API Key 恒为 NULL）
  revoked_at    TEXT,          -- NULL = 有效
  rotated_from  INTEGER,       -- refresh 轮换来源，便于审计
  FOREIGN KEY (rotated_from) REFERENCES api_credentials(id)
);
CREATE INDEX idx_api_credentials_public_id ON api_credentials(public_id);
-- 任意时间最多一条有效 API Key：
CREATE UNIQUE INDEX idx_api_credentials_single_key
  ON api_credentials(kind) WHERE kind = 'api_key' AND revoked_at IS NULL;
```

- **默认不生成**：启动/迁移/初始化代码都不插入 `api_key` 行；全新部署就是没有 Key。
- 刷新（换新）不物理删旧行：写 `revoked_at`，再插新行——保留「上次使用」审计，
  且部分唯一索引保证不会有第二条有效行。
- `label` 只给登录会话命名设备；API Key 也记一个固定标签（如「移动端」）。
- 列表/快照只含元数据（`public_id`、创建时间、最近使用），**没有**回显 secret 的路径。

### 4.3 新增端点

新模块 `app/api/auth.py`，挂 `/api/v1/auth`：

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/v1/auth/login` | 无 | body `{password, label?}` → `{access_token, refresh_token, expires_in, token_type}` |
| POST | `/api/v1/auth/refresh` | 凭 refresh token | body `{refresh_token}` → 新 access + **轮换** refresh（旧的立即吊销） |
| POST | `/api/v1/auth/logout` | bearer | 吊销本设备的一族 token（不动 API Key） |
| GET | `/api/v1/auth/whoami` | bearer | `{username, kind, expires_at, must_change_password:false}`；客户端「测试连接」用 |

- 密码校验复用 `app.state.password_hasher.verify` 与 `admin_users`，不新写一套。
- 失败锁定复用：把 `login_attempts` 的桶、`_prune_expired`、`MAX_*` 抽到
  `app/web/login_throttle.py`，网页登录与 JSON 登录共用，两个入口的 429 语义一致。
- 未改初始密码时 `login` 返回 403 `PASSWORD_CHANGE_REQUIRED`，**不签发 token**；
  客户端提示「请先在网页端修改初始密码」。
- 新错误码：`AUTH_INVALID_CREDENTIALS`、`AUTH_TOKEN_EXPIRED`、`AUTH_TOKEN_REVOKED`；
  响应信封沿用 `ApiError`。

**API Key 管理只在网页端**（会话 + CSRF），移动端不提供签发接口。
密码库 tab 的单 Key 面板走网页表单（与 `settings_pages.py:1327` 归档密码同款）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/settings/api-keys/generate` | 生成；若已存在有效 Key，页面先给危险确认（旧 Key 立即失效），确认后**刷新**=吊销旧+生成新 |
| POST | `/settings/api-keys/revoke` | 撤销当前 Key（无可用 Key 时不可点） |

- 明文只在生成/刷新响应里出现**一次**（re-render 密码库 tab，一次性展示区 + 复制按钮），
  列表只留元数据；这一条写进模板注释，防止后来者加回显路径。
- 元数据加进 `settings_snapshot`（`app/api/settings.py`），模板
  `settings/_passwords.html` 增第三个面板「API 密钥（移动端）」。
- **不新增设置分区**：`SETTINGS_SECTIONS` / `nav_items` / 手机 tab bar 都不动。

### 4.4 统一鉴权入口（`app/api/deps.py`）

把 `require_session` 改造成「会话 **或** bearer」，**函数名与 24 个调用点不动**：

```python
def require_session(request):
    credential = _bearer_credential(request)   # 解析 Authorization + 查表 + 验哈希
    if credential is not None:
        request.state.auth_source = "bearer"
        request.state.api_credential = credential
        return                                  # 完整权限；签发时已保证改过密码
    if not request.session.get("authenticated"):
        raise ApiError("NOT_AUTHENTICATED", "请先登录", status_code=401)
    if request.session.get("must_change_password"):
        raise ApiError("PASSWORD_CHANGE_REQUIRED", "请先修改初始密码", status_code=403)
```

`require_csrf` 开头加一行：

```python
if getattr(request.state, "auth_source", None) == "bearer":
    return          # Authorization 凭据不来自 cookie，跨站请求带不上，CSRF 不适用
```

结果：浏览器路径字节不变；移动端对全部既有 `/api/v1` 端点即刻可用；防伪造性质保留。

### 4.5 单 Key 的运维语义

- **刷新会使旧 Key 立刻失效**：所有用旧 Key 的设备要重新粘贴新 Key——面板上明确写出。
- **刷新只影响 Key 认证的客户端**：密码登录得到的 access/refresh 完全不受影响（§4.6）。
- 没有 Key 时，移动端的「API Key 登录」直接给「请先在网页端生成」的指引。
- 只暴露 `public_id` 前 8 位与「最近使用」，便于识别，不足以重放。

### 4.6 凭据族隔离（改密 / 刷新 Key 的边界）

两个族**完全独立**，一个动作只作用于自己那一族：

| 动作 | 密码认证族（access/refresh） | API Key 族（`kind='api_key'`） |
|------|------------------------------|-------------------------------|
| 改管理员密码 | **整族吊销**，其它设备重新登录 | 不受影响（你的决定） |
| 网页「刷新 API Key」 | 不受影响 | 旧 Key 立即失效，用旧 Key 的客户端 401 |
| 网页「撤销 API Key」 | 不受影响 | 当前 Key 失效 |
| 移动端 logout | 只吊销本设备的 access/refresh | 不受影响 |
| 网页退出登录 | 只清浏览器 cookie 会话 | 不受影响 |

- 族由**获取方式**决定，不靠推断：`kind` 已区分 `api_key` 与 `access`/`refresh`，
  轮换与吊销都按 `kind` 过滤，不存在交叉。
- 改密吊销的是**整族**（access **和** refresh）：只吊销 access 不吊销 refresh 等于没吊销
  ——下一次 refresh 立刻换回可用 access。这按工程上唯一成立的做法实现；
  若你只要「吊销 access」的字面语义请指出（那要额外把 refresh 也置于待换绑状态）。
- 网页 cookie 会话与移动端两族同样互不影响。

### 4.7 备选（否决）

- **只做 API Key**：无法表达「短期会话」，改密后无法让设备重新认证。保留 Key 作一条腿。
- **JWT 无状态**：需引入 PyJWT；仓库刻意只有 pwdlib 一个认证依赖
  （`app/archive/vault.py` 手写 envelope 正因此），且吊销要黑名单。否决。
- **cookie + CSRF 照搬**：跨平台 cookie 行为不一致，且 `/api/v1` 的 401/303 语义为 fetch 设计。否决。
- **OAuth device flow**：单人自托管，无第三方 IdP。否决。

---

## 5. 缩略图方案分析（本项目最合适的选择）

### 5.1 现状与约束

- `GET /api/v1/thumbnails/{hash}`（`app/api/thumbnails.py`）目前 `require_session`，
  P1 之后自动接受 bearer。
- URL **只承载内容哈希**（`_HASH_PATTERN` = 64 位 hex）；服务端不接受调用方提供的 URL，
  这正是它不做开放代理的原因。
- 就绪：`Cache-Control: private, max-age=31536000, immutable`、`ETag: "<hash>"`、支持 304。
- 失败：**200 + `thumb-placeholder.svg`**（SVG！）+ `X-Thumbnail-State: failed`
  + 可选 `X-Thumbnail-Error`。用 200 是为了让 `<img>` 不出现破图图标。
- 浏览器靠 `<img src>` 自动带 session cookie；原生端没有这个「自动带凭据」的等价物。

### 5.2 选项对比

| 方案 | 后端改动 | 客户端 | 代价 |
|------|----------|--------|------|
| **A. 客户端带鉴权的图片加载器**（推荐） | 零 | 一个 `ThumbnailImageProvider`：带 `Authorization`、发 `If-None-Match`、读 `X-Thumbnail-State`、按哈希做磁盘缓存 | 约 150 行客户端代码 |
| A′. `cached_network_image` + `httpHeaders` | 零 | 第三方图片缓存包 | 读不到 `X-Thumbnail-State`；SVG 占位图解码失败走 `errorWidget`；多一个依赖 |
| B. 缩略图 URL 附加短期签名 query | 新签名/校验 + TTL | 普通 `Image.network` 即可 | 破坏「URL 即内容哈希」与 `immutable` 缓存；token 会进日志/referrer；新增 crypto 面 |
| C. 缩略图改为公开 | 去掉鉴权 | 零 | 拿到哈希即可读封面；顺带降低浏览器端安全性，R18 私有部署不可接受 |
| D. 后端改发 PNG 占位图 / payload 带 state | 有 | 普通 `Image.network` 可解码 | 解决的是占位图问题，不是鉴权问题；可作可选后续 |

### 5.3 结论（已采纳）：A —— 客户端带鉴权的图片加载器，后端零改动

理由：

1. **顺着现有设计走**：后端把 URL 做成不可变内容哈希（`immutable` + ETag），
   缺的只是「原生端无法自动带凭据」。统一 deps 之后这一点已经解决，**不需要新协议**。
2. **缓存不被鉴权污染**：缓存键是哈希/URL，token 轮换不会打穿本地缓存；
   若用方案 B，签名参数会随每次刷新变化，等于放弃那份「缓存一年」的设计。
3. **能正确处理占位图**：`X-Thumbnail-State: failed` 时直接渲染本地占位组件，
   不去解码 SVG（Flutter 的位图解码器不认 SVG），互不将就。
4. **兼容 ETag/304**：加载器可以带 `If-None-Match: "<hash>"`，二次进入页面省一次传输。

实现要点（移动端）：

- `ThumbnailImageProvider` 复用 App 的鉴权 HTTP 客户端（自带 401→refresh 拦截器），
  请求头带 `Authorization`；响应头有 `X-Thumbnail-State`。
- 就绪响应按 `sha256(源)` 哈希为文件名写入应用缓存目录（`path_provider`），
  命中直接读盘；内存层用 `ImageCache`。
- `state=failed` 或请求失败 → 本地占位组件（不重试成风暴；失败态后端已给 `max-age=60`）。
- 列表滚动用 `ListView.builder` 懒加载，避免一次并发几十张。

**A′ 只在「想少写代码」时选**：用 `cached_network_image` + `httpHeaders`，
接受 SVG 占位走 `errorWidget`、读不到失败态头。可作为 A 的降级路径。

---

## 6. Flutter 客户端设计

新仓库 `/home/coder/workspace/ehbot-mobile`，包名 `ehbot_mobile`（可改），
`lib/api/`（client、auth、models）、`lib/state/`、`lib/ui/`、`lib/media/`（缩略图加载器）。

- **服务器配置**：`base_url`（scheme + host + port + 可选 path 前缀，对应 `APP_ROOT_PATH`）、
  「测试连接」（`GET /healthz` 判可达 → `GET /api/v1/auth/whoami` 判凭据）、超时。
  TLS 默认系统信任；「允许自签证书」开关（保存指纹 pin，明确告警）；
  明文 `http` 需 Android `network_security_config` + iOS ATS 例外，UI 标红提示。
- **登录页两个 tab**：
  1. 密码：`POST /api/v1/auth/login`，`refresh_token` 存 `flutter_secure_storage`
     （Keychain/Keystore），access 仅内存；401 自动 refresh 一次，失败回登录页。
  2. API Key：粘贴 `ehk_...`，存 secure storage，不参与刷新；
     401 直接提示「密钥无效或已在网页端刷新」。
- **数据**：`/api/v1/summary|candidates|works|activity|downloaded|meta`。
- **实时**：`/api/v1/events` 是 SSE，Dart HTTP 客户端可带 `Authorization` 头消费
  （原生端相对浏览器 `EventSource` 的优势）；断流按 `meta.polling` 回退轮询。
- **缩略图**：§5 方案 A。
- **权限**：Bearer 等同管理员，写操作无需额外改动（§4.4 已放行 CSRF）。

---

## 7. 「基本可用」定义与分期计划

### 7.1 MVP 验收线（本目标）

**装 App → 配服务器 → 用密码或 API Key 登录 → 看到工作台/候选/活动/已下载四块数据 →
能对候选通过/驳回 → 封面正常显示 → 进程被杀后重开仍登录（refresh/Key）→ 断流能自恢复。**

满足以上即「基本可用」，可作为 v0.1.0 交付给运营者试用。

**MVP 明确不做**（后续版本）：设置页的编辑（来源规则、AI、归档、路径）、批量操作、
日志实时流查看、推送通知、主题/无障碍精修、iOS 商店签名发布。

### 7.2 分期（每期一个 commit，文档同步是交付的一部分）

| 期 | 仓库 | 内容 | 完成判据 |
|----|------|------|----------|
| **B1** | EhBot | 迁移 `023` + `app/api/auth.py` + `deps.py` 统一 + `login_throttle.py` 抽取 + 测试 | curl 能用 token 访问全部 `/api/v1`；浏览器用例全过 |
| **B2** | EhBot | 密码库单 Key 面板 + 网页表单路由 + `settings_snapshot` + 测试 + 文档 | 网页能生成/刷新/撤销唯一 Key；明文只显示一次 |
| **M0** | ehbot-mobile | 建仓库 + §8 的 agent 文档骨架 + Flutter 工程 + CI（`flutter analyze`/`flutter test`） | `flutter test` 绿；文档结构与 EhBot 对齐 |
| **M1** | ehbot-mobile | 服务器配置页 + 两种登录 + `flutter_secure_storage` + 401 刷新拦截器 | 两种方式都能登录并保住会话 |
| **M2** | ehbot-mobile | 缩略图加载器（§5 A）+ 缓存 | 封面/占位图正确，翻页命中缓存 |
| **M3** | ehbot-mobile | 工作台、候选列表/详情、通过/驳回、活动/队列、已下载列表 | §7.1 验收线全部走通 |
| **M4** | ehbot-mobile | SSE + 轮询回退 + 错误/离线态 + 基础打磨 | 断流自恢复；v0.1.0 可试用 |

B1/B2 与原仓库发布节奏一致（`latest` 镜像）；M0–M4 在新仓库独立推进。
**本期不建议**把设置编辑/批量等塞进 MVP，先把主链路做实。

---

## 8. 新仓库的 agent 文档结构（复刻 EhBot）

在 `/home/coder/workspace/ehbot-mobile` 建立与 EhBot 同构的管理方式：

```
ehbot-mobile/
├── AGENTS.md                 # 指针 stub：先读 AgentHelp/AGENTS.md（与 EhBot 根一致）
├── README.md                 # 面向使用者：怎么构建、怎么填服务器、怎么登录
├── .gitignore                # build/、.dart_tool/、.codegraph/、密钥文件等
└── AgentHelp/
    ├── AGENTS.md             # 环境约束、架构规则、业务不变量、测试基线、工作约定
    ├── PHASES.md             # 每期一行 + 指向 progress.md 的行号（读这份最先）
    ├── progress.md           # M 编号的分期实施日志（对照 EhBot 的 R 编号格式）
    ├── MOBILE.md             # 移动端需求规格（对应 EhBot 的 EHBot.md）
    ├── DEVELOPMENT_PLAN.md   # 分期计划与 MVP 验收线
    ├── CONTRACT.md           # 依赖的后端契约：openapi.json 版本、错误码、信封
    └── *_PROPOSAL.md         # 每个功能的设计记录（先方案后开工）
```

规则照搬，但工具命令换成 Dart/Flutter 侧：

- **先方案后开工**：任何可见行为改动先写 `AgentHelp/<FEATURE>_PROPOSAL.md`，审阅后动手。
- **文档同步是交付的一部分**：功能/界面一变就改 `README.md`，每期在 `progress.md`
  追加 M 编号条目并更新 `PHASES.md` 与 `AGENTS.md` 的测试基线链。
- **测试基线**：Flutter 用 `flutter test` 的用例数与 `flutter analyze` 零告警，
  写法与 EhBot 的「N collected / 0 failed」对应。
- **codegraph 的差异（诚实写明）**：`codegraph doctor` 的原生语法列表**不含 Dart**
  （c/cpp/csharp/css/go/html/java/js/kotlin/less/php/python/ruby/rust/scss/sql/swift/ts/tsx/zig）。
  所以新仓库的 `AGENTS.md` 要写清楚：**Dart 代码用 IDE / `dart analyze` / `grep` 定位**，
  `codegraph sync` 只对仓库里的 Markdown/配置有有限价值，不把它当作导航主力。
- **后端契约**：`AgentHelp/CONTRACT.md` 记录所依赖的 `openapi.json` 快照与最低后端版本，
  后端契约一变，移动端先看这份再评估。

---

## 9. 契约与兼容

- FastAPI 默认暴露 `/openapi.json`（`app/main.py:76` 未关闭 `openapi_url`），
  作为 release artifact / 契约快照；移动端加「契约快照未变」测试。
- 信封已固定：错误 `{error:{code,message,details}}`；列表
  `{items,total,page,page_size,pages}`（`app/api/contracts.py`）。
- **不加** `api.version`（已定）：客户端以「`POST /api/v1/auth/login` 是否返回业务响应
  而不是 404」探测后端是否已支持移动端鉴权；契约快照仍以 `openapi.json` 为准。

---

## 10. 安全

- **传输**：建议 HTTPS；局域网明文需显式确认；密码/token 绝不入日志。
- **存储**：服务端只存 sha256；移动端存 Keychain/Keystore；网页明文只显示一次。
- **单 Key 的风险与代价**：一条 Key 全权且不过期——泄露影响面等于管理员。
  缓解：密码库页常驻「最近使用」+ 一键撤销；生成时提示旧 Key 立即失效。
  这是你选定的取舍，方案如实标注。
- **限流**：密码登录沿用 IP 桶；Bearer 失败记 `API_CREDENTIAL_REJECTED`
  （只带 `public_id` 前 8 位，不带 secret）。

---

## 11. 测试计划

- **单元**：token 生成/解析/哈希/过期/吊销/refresh 轮换；单 Key 唯一性
  （生成第二条前旧行必须已撤销）；共享 throttle。
- **集成**：密码登录成功/失败/锁定/未改密；Bearer 访问每个域各一个代表端点；
  Bearer 时 `require_csrf` 放行、cookie 会话时仍强制；Key 生成/刷新/撤销；
  撤销后立即 401；**族隔离**：改密后 access/refresh 失效而 Key 仍可用，刷新/撤销 Key 后
  密码认证的 access/refresh 仍可用；
  `whoami`。
- **回归**：现有 1690 基线不减；浏览器登录/CSRF 用例原样通过。
- **移动端**：契约快照测试 + widget 测试（两种登录、401 自动刷新、刷新失败回登录、
  缩略图占位态）。

---

## 12. 文档同步（交付必做）

- `README.md` / `docs/USAGE.md`：新增「移动客户端」与「API 鉴权 / API Key」一节；
  环境变量若有增删同步 `.env.example`（本方案默认不加 env）。
- `AgentHelp/EHBot.md`：需求侧补移动端、API Key 管理、完整权限。
- `AgentHelp/progress.md`：B1、B2 各一条 R 条目（预计 R53、R54）。
- `AgentHelp/PHASES.md`：追加行 + 基线链。
- `AgentHelp/AGENTS.md`：基线数字更新。
- 移动端仓库：按 §8 建立自己的 `AgentHelp/` 全套。

---

## 13. 待确认项

**已定（2026-10-03 审阅通过，按本方案实施）**：

1. 凭据族隔离：改密只吊销密码认证的 access/refresh；刷新/撤销 Key 只影响 Key 认证的客户端（§4.6）。
2. 缩略图采用 §5 方案 **A**（客户端带鉴权加载器，后端零改动）。
3. access 12h / refresh 30d，放「设置 › 系统」可调。
4. `/api/v1/meta` **不**加 `api.version`；客户端靠 `POST /api/v1/auth/login` 是否 404 探测后端是否就绪（§9）。
5. 仓库 `ehbot-mobile`、包名 `ehbot_mobile`。
6. 本期按 §7 排到基本可用：B1、B2、M0–M4。
