# 方案：Web 登录表单被 API 同名路由劫持（回归修复草案）

> 状态：**已批准并实现（R57，2026-10-06）**。回应运营者 2026-10-06 的
> 报告：「web ui 使用密码登陆时出现上面的异常」——日志为
> `POST /api/v1/auth/login` `BODY_INVALID`「请求体必须是 JSON 对象」。

## 0. 结论先说

根因是**跨层路由函数名冲突**，不是鉴权逻辑问题。

- `app/api/auth.py:115` 的 `async def login(...)` 注册为 `POST /api/v1/auth/login`，路由名 `login`。
- `app/web/routes/auth.py:50` 的 `async def login(...)` 注册为 `POST /login`，路由名也叫 `login`。
- `app/main.py:135` 先 `include_router(api_v1_router)`，`:155` 才 `include_router(auth_router)`（Web），
  而 `url_for('login')` 返回**最先注册**的同名路由，于是解析到 `/api/v1/auth/login`。
- `app/web/templates/login.html:58` 因此渲染成
  `action="/api/v1/auth/login"`；浏览器按表单提交 `application/x-www-form-urlencoded`，
  JSON 端点的 `_json_body`（`app/api/auth.py:51`）解不出 JSON，抛 `BODY_INVALID`。
  页面永远停留在错误响应上——**Web UI 密码登录完全不可用**。

引入于 **R53**（提交 `3639561`，v0.3.0rc2，2026-10-03），即移动端鉴权新增 API 登录路由时。

## 1. 复现与证据（本机）

```text
$ .venv/bin/python -c "from app.main import create_app; print(create_app().url_path_for('login'))"
/api/v1/auth/login

$ TestClient(app).get('/login')  ->  <form ... action="http://testserver/api/v1/auth/login">
```

- `git log -S "async def login(request: Request) -> dict[str, object]" -- app/api/auth.py`
  只有 `3639561`（R53）；此前不存在第二个 `login`，`url_for('login')` 正常指向 `/login`。
- 日志三条 `POST /api/v1/auth/login BODY_INVALID`（11:23–11:24）与运营者手工登录的时间吻合。

## 2. 影响面

- **Web 页面密码登录**：自 v0.3.0rc2 起不可用。已发布镜像（R55 `v0.3.0rc3`）带此回归。
- **不受影响**：`GET /login`、会话 cookie 本身、鉴权依赖；既有集成测试因为直接
  `POST /login`，从不经过模板渲染的 action，所以一路全绿没发现。
- **同类同名路由（潜在陷阱，暂未触发）**：`logout`（`app/api/auth.py:204` / `app/web/routes/auth.py:183`）、
  `batch_review`（`app/api/actions.py:343` / `app/web/routes/candidates.py:397`）。
  它们目前都用硬编码路径（`base.html:110` 的 `/logout`、`candidates.html:183` 的
  `/candidates/batch-review`），所以没爆；但任何人一旦改用 `url_for`，就会踩同一个坑。

## 3. 修法（推荐）

**让 Web 侧保留 `login` / `logout` / `batch_review` 这三个「页面名」，给 API 侧同名函数改名**
（函数名即默认路由名）：

- `app/api/auth.py`：`login` → `api_login`，`logout` → `api_logout`
- `app/api/actions.py`：`batch_review` → `api_batch_review`

不改 URL、不改模板、不改请求/响应，只改路由名，`url_for('login')` 自然回到 `/login`。

**备选（不采用）**

- 只改 `login`：最小改动，但把 `logout` / `batch_review` 两颗地雷留在原地。
- 把 Web `auth_router` 提到 `api_v1_router` 之前：与 AGENTS.md「API 先注册，路径不互相遮蔽」的理由冲突，
  靠注册顺序治名字冲突太脆。
- 模板写死 `action="/login"`：绕过症状、不解决同名根因，下一个 `url_for` 又会中招。

## 4. 测试计划（+2，基线 1741 → 1743）

- 新增：`create_app().url_path_for('login') == '/login'`，并把 `logout` 一起钉住
  （回归测试，防止再次被 API 路由抢占）。
- 新增：`GET /login` 渲染出的表单 `action` 为 `/login`（直接断言模板产物，而不是只测 POST 路径）。
- 既有 Web 登录/锁定/改密与 API 鉴权用例保持全绿。

## 5. 文档同步

- `progress.md` 追加 R57 条目（回归根因、修法、测试基线），`PHASES.md` 加一行，`AGENTS.md` 基线链 1741 → 1743。
- `README.md` / `docs/USAGE.md` 不新增描述：这是恢复既有行为，不是新功能；提交说明写「无用户可见文档变化」。
- 建议在 `AGENTS.md` 架构规则补一句：跨 `app/api` 与 `app/web/routes` 的路由处理函数名必须唯一，
  因为 `url_for(name)` 取最先注册者——这正是本次回归的教训。
