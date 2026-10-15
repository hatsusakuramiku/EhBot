# 实时 API：ehbot-realtime.v1（R86 / R87）

本契约已在服务端和 WebUI 实现。EhBot-Mobile 后续接入本协议，本轮不修改移动端代码。
服务端版本号仍为 `0.3.0rc3`，判断能力应请求 `GET /api/v1/realtime`，不能仅凭版本号判断。
此接口需认证，返回协议、带部署前缀的 WebSocket 路径、命令目录、epoch、sequence 和心跳间隔。

## 连接与认证

- 路径：`/ws/ui`；有 `APP_ROOT_PATH=/ehbot` 时是 `/ehbot/ws/ui`。HTTP 对应 `ws`，HTTPS 对应 `wss`，与服务器使用相同主机/端口。
- WebUI：已有 Session Cookie，同源 `Origin`，hello 携带当前页面 CSRF token。
- 原生客户端：握手请求头 `Authorization: Bearer <access token 或 API Key>`，无需 Cookie/CSRF/Origin。不把凭据放在 URL、帧、日志或本地可分享的连接配置中。
- refresh token 仅用于 HTTP 刷新接口，不能用于此连接。保留 `/api/v1/auth/login|refresh|logout|whoami`。
- 认证失败在升级前关闭；协议/CSRF 错误在升级后返回 `error` 并关闭。活跃连接每条命令及心跳重新验证凭据；到期/撤销返回现有认证错误码并关闭。Cookie 会话有服务端设备记录及 14 天绝对期限；注销只结束该设备及其 WS，改密码结束所有旧密码会话及其 WS，改名保留会话。
- HTTP `auth/login` 要求 `username`＋`password`；profile GET/PATCH 与 change-password POST 见 USAGE。认证设置由 HTTP 提交，不进入 WS 命令目录。schema 026 升级前的 Cookie 需重新登录。

第一帧在 10 秒内发送 JSON 文本：

```json
{"type":"hello","protocol":"ehbot-realtime.v1","client":"mobile","logs":false}
```

浏览器另加 `"csrf_token":"页面 token"` 和 `"client":"web"`。`logs:true` 同时订阅日志。
服务端回复：

```json
{"type":"ready","protocol":"ehbot-realtime.v1","epoch":"随机进程标识","sequence":12,"heartbeat_seconds":15,"replay":false,"commands":{"api.convert_work":{"method":"POST","path":"/api/v1/works/{candidate_id}/convert","params":["candidate_id"],"encoding":"json"}}}
```

实际 commands 包含全部已注册的有限 JSON 操作和快照；WebUI 还包括 `web.*` 表单/片段命令。
认证、流、缩略图和文件传输不作为命令，继续使用 HTTP。原生客户端只能使用 `api.*`。
命令目录来自明确注册的业务路由，无法传入任意 URL、方法、模块或函数。命令名属于 v1 契约，改名须同步契约及客户端。

## 提交操作与读取快照

```json
{"type":"command","request_id":"pack-42-1","name":"api.convert_work","payload":{"params":{"candidate_id":"42"}}}
```

审核示例：

```json
{"type":"command","request_id":"review-1","name":"api.api_batch_review","payload":{"body":{"action":"approve","candidate_ids":[42,43]}}}
```

元数据与队列快照示例：

```json
{"type":"command","request_id":"edit-42","name":"api.patch_metadata","payload":{"params":{"candidate_id":"42"},"body":{"fields":{"Series":"系列名称"}}}}
{"type":"command","request_id":"queue-1","name":"api.get_queue","payload":{"query":[["page_size","100"]]}}
```

`payload.params` 必须完整对应目录中的 params，值使用字符串；单个参数不可包含路径分隔符。
`body` 对 JSON 命令使用原 HTTP API 请求体；`query` 使用字符串二元组数组，支持重复筛选条件。
API 的字段、状态视图、批量限制、审核/删除/文件门禁及错误码与 HTTP 相同。
WebUI 的 `body` 使用表单二元组数组，服务器注入可信 CSRF，`context` 保存当前筛选查询；移动端无需这些 HTML 细节。

服务端先回复已接收，再回复结果或错误：

```json
{"type":"command.accepted","request_id":"pack-42-1"}
{"type":"command.result","request_id":"pack-42-1","status":200,"format":"json","data":{"candidate_id":42,"job_id":9},"epoch":"随机进程标识","sequence":12,"location":"/api/v1/works/42/convert"}
{"type":"command.error","request_id":"edit-42","status":400,"format":"json","data":{"error":{"code":"FIELD_UNKNOWN","message":"业务提示","details":{}}},"error":{"code":"FIELD_UNKNOWN","message":"业务提示","details":{}}}
```

示例 data 只示意信封；实际 data 完整复用原接口响应。格式/路径字段是结果描述，客户端不能据此执行任意路径。
`accepted` 表示排入该连接的处理队列；`result` 表示接口完成，打包操作通常只是任务入队，后续执行/完成/失败通过状态事件获知。
无 HTTP 响应的协议错误只包含 `request_id`（关联命令时）、`error.code/message/details`，status/data 可缺省。

连接内命令依次执行，最多积压 8 条；`COMMAND_BUSY` 表示未入队。
每条 request_id 为 1–128 字符的非空字符串，在同一连接最近 4096 条内重复会收到 `COMMAND_DUPLICATE`，不会再次执行。
不同连接及缓存窗口之外不提供持久化幂等保证。客户端应生成唯一 request_id，**断线/超时后不自动重发写操作**；先读取业务快照确认状态，再由用户决定下一步。
帧大小上限 256 KiB，二进制帧、无效 JSON 和未知协议被拒绝。

## 状态事件与一致性

```json
{"type":"state","epoch":"随机进程标识","sequence":13,"scope":"conversion","data":{"job_id":9,"candidate_id":42},"refresh_required":true}
```

scope 是 `candidate/download/conversion/connection/refresh`。data 的实体 ID 按事件可用性提供；
`refresh` 的 data.path 是完成操作的业务路径，不含查询值或凭据。
这些是**失效通知**，并非另一份实体状态；收到相关事件后按当前筛选读取 JSON 快照，显示状态词表或快照中的标签/颜色。
服务端覆盖新候选入库、元数据抓取、自动审核、下载/打包认领及结束、种子进度、连接恢复/失败，以及 HTTP/WS 写操作。

EventBus 在订阅建立前已开始计数；每次服务重启更换 epoch。每个订阅队列最多 64 条，慢客户端丢最旧事件，业务任务不等待前端。
没有持久化事件重放：每次 ready/重连均读取快照；sequence 跳跃或 heartbeat 水位超前时再校正。
命令快照结果的 sequence 是**读取开始前的保守水位**，不是持久化实体 revision；读取期间到达的较新事件不能被此响应吞掉。
先订阅再读快照，缓冲/合并期间的失效通知；若通知序列超过响应水位，再读一次。
只保留最新请求的结果，切服务器、页面或筛选时丢弃旧请求，禁止旧服务器的结果写入新视图。
不要同时把全局 sequence 当作作品、任务或日志的独立版本。

每 15 秒发送 `{"type":"ping","epoch":"...","sequence":13}`，客户端回复 `{"type":"pong"}`。
45 秒未收到客户端消息会断开。发送超时 10 秒也结束连接并清理订阅。
断线采用抖动指数退避（建议 1 秒起、上限 30 秒），保留当前页面内容，不恢复定时页面轮询。
access 失效按原 HTTP 流程刷新一次并重连；API Key 失效提示重新认证，不能尝试 refresh。
App 回到前台重新连接/校正，切服务器和退出先关闭旧连接并清理未完成请求。

## 日志与 HTTP 保留入口

`logs:true` 订阅同一连接的日志，先回放内存中最近 50 条，随后发送：

```json
{"type":"log","epoch":"随机进程标识","sequence":81,"data":{"level":{"code":"INFO","label":"信息","tone":"neutral"},"event":"已脱敏日志","timestamp":"..."}}
```

data 使用 `GET /api/v1/logs` 相同条目格式，沿用统一脱敏链。日志 sequence 独立于状态 sequence；按 `(epoch,sequence)` 去重。
日志快照新增 `log_epoch`、`log_sequence`，对应本次读取的内存缓冲水位；缓冲条目附 `sequence`，磁盘历史条目不保证有序号。
快照读取期间缓存到达的日志，渲染快照后仅补上同一 epoch 中超过 `log_sequence` 的记录，避免回放重复或覆盖新日志；旧筛选请求的响应必须丢弃。
回放有上限，不能保证补齐任意时长的断线；序列缺口或重连使用日志快照校正。暂停时缓存有界，级别是下限。
状态/日志 SSE HTTP 入口保留供既有测试客户端过渡；R86 WebUI 已没有 EventSource 或页面状态定时轮询。
保留所有既有 `/api/v1` JSON、登录、缩略图和文件接口；后台采集 Telegram/qBittorrent 的轮询及心跳继续运行。
`GET /api/v1/events/stats` 报告事件订阅数和丢弃数；日志快照也提供日志订阅与丢弃统计。

## 反向代理

nginx 的对应 location 至少要保留升级头和足够的读超时；root_path 要与代理前缀一致：

```nginx
location / {
    proxy_pass http://ehbot:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 75s;
    proxy_buffering off;
}
```

HTTPS 由代理终止时，推荐在 Compose 设置 `PUBLIC_BASE_URL: "https://ehbot.example"`。
它固定公开协议、主机及端口，用于 HTTP/WS URL 生成、Origin 校验和内部命令桥；无需启用代理头信任或填写 Docker 网关 IP。
空值保留直连及原可信代理行为。它不信任 X-Forwarded-For，不改变实际客户端来源；
显式开启 `TRUST_PROXY_HEADERS` 时仍需独立设置 `TRUSTED_PROXY_IPS`。部署前缀仅用 `APP_ROOT_PATH`。
Cookie Origin 按域名大小写和默认端口规范化后精确比较，协议、子域名、其他端口均不能混用。
不要给 WS 另造不匹配的域名，或把 API Key 拼进 proxy_pass/访问日志。

宿主机 Caddy → Docker 发布端口可直接 `reverse_proxy 127.0.0.1:8080`，Caddy 自动处理升级。
外层 Cloudflare 保持 Full (strict)；关闭该站点 Web Analytics 自动脚本注入，避免 beacon.min.js 被应用 CSP 拒绝。
应用仅为当前入口加入明确的 WS/WSS connect-src，不放宽脚本源。502 是独立的回源故障，应检查上游端口和服务状态。

## R87 命令与元数据补充

自动审核 API 命令名修正为 `api.api_toggle_auto_approval_rule` 和 `api.api_delete_auto_approval_rule`，
HTTP URL 与 PUT/DELETE 方法不变；Web 命令仍为 `web.toggle_auto_approval_rule`、`web.delete_auto_approval_rule`。
客户端从 commands 目录按 path/method 找对应操作，不能猜同名 Web 方法。
验证拒绝的 command.error.error.message 包含实际字段/方法原因，JSON 错误不作为 HTML 片段替换内容。

`api.get_work` 返回 `metadata_editor`（`packaged/path/error/synced_at/fields/fetched_fields`）。
fields 使用 `field_name/field_label/field_value/editable/source/is_manual/is_locked/lock_field/lock_available`；
归档字段已打包取文件值，未打包取打包投影。非归档自定义字段同列显示，`archive_field:false`。
保留既有 metadata/raw_metadata/archive_metadata 供来源查询；archive_metadata.elements.field_value 也修正为当前编辑值。

`api.fetch_work_metadata` 持久化拉取内容并返回上述编辑快照；`fetched_fields` 只列可覆盖的非手动、非锁定字段。
已打包客户端将这些值合入当前草稿，保护本地未保存输入，拉取本身不改文件。
未打包直接作为下次打包数据；已打包重新进入页面仍读取实际文件。

```json
{"type":"command","request_id":"save-fetch-42","name":"api.patch_metadata","payload":{"params":{"candidate_id":"42"},"body":{"fetched_fields":{"Title":"拉取的标题"},"fields":{"Translator":"手动更改的译者"}}}}
```

允许仅提交 fetched_fields，值取自拉取结果；fields 才记录为手改，locks 使用快照给出的 lock_field。
保存只应用本次变化，保留未改 XML、生成事实、页结构、图片和路径。
响应继续提供 archive_synced/sync_skipped/sync_message，并附 metadata_editor；
锁操作或仅数据库字段可返回 `NO_ARCHIVE_CHANGES`。写回失败/跳过时保持草稿，不自动重发。
成功保存后重新读取快照；后台状态事件只触发读取，不能隐式保存草稿。

## 验证命令

P2 / schema 028（任务契约从 027 引入）：`api.job_action`、`api.set_priority`、`api.switch_source` 的 `payload.body` 必须包含
`task_id`、`expected_revision`（正整数）、`idempotency_key`（1–200 字符）。读取 `/api/v1/queue`、作品详情或
`/api/v1/tasks/{task_id}` 得到任务身份与版本，不能使用旧整数编号代替 UUID；HTTP、WS、普通表单共享相同裁决。

```json
{"type":"command","request_id":"pause-1","name":"api.job_action","payload":{"params":{"job_id":"42","action":"pause"},"body":{"task_id":"已读取的任务UUID","expected_revision":3,"idempotency_key":"本次意图的唯一键"}}}
```

`api.batch_job_action` 仍传 `job_ids`，另传 `preconditions` 对象：键是每项整数编号的字符串，值为上述三个字段。
每项独立裁决；applied.result 是命令结果对象，skipped 明确拒绝原因。所有设备/认证凭据共享固定管理员幂等作用域。
同键同内容先返回回执；同键异内容、旧版本或旧任务身份返回 409（IDEMPOTENCY_CONFLICT / REVISION_CONFLICT / TASK_SUPERSEDED）。
回执与状态/操作同事务；已受理未确认的操作返回 operation_id，不能将 WS command.result 当作物理停止完成。
回执保留七天，尚未结束的操作回执不清理；超出保留期先核对任务现状，再决定是否新发命令。
任务详情的已结束 attempt 保留执行事实；后续远端核实的状态在 task/operation 中表达，不将旧失败改成 STOPPING 或抹掉旧错误。

STOPPING 保留作品槽。已验收 P3 / schema 029 接入本地请求/线程/进程树收束与远端推送查回/归属核实；确认停止后才完成 operation 并入队切源/重试目标。推送结果未知、失联或实例/归属改变时 operation 保持未完成并记录 error_code；新共享种子拒绝附着，轮询核对原实例和本 attempt 标签后才写进度/产物。运行移除立即删除业务行，返回 `removal_id`、`operation_ids`、`cleanup_status`，独立收尾继续保留。`GET /api/v1/removals/{removal_id}` 复用 Cookie/Bearer 鉴权，返回状态、关联任务、停止错误与 deleted_files/failed_files；业务候选已删除时仍可查。任务详情新增 `removals`，其 PENDING 表示独立作品预留尚未释放，不能用任务的 REMOVED 推断下载已停止。默认保留文件，同作品新任务排队等待停止和文件清理确认。完整发布崩溃恢复、心跳/预算按 P8/P9 接续。旧客户端缺少前置条件直接拒绝，不自动采用服务器最新版本。

`switch-source` 先读取管理员作用域回执，再在数据库写事务外验证目标凭据/链接与页面可读性；不下载目标原档或图片。最终受理事务再次核对 task/revision 和目标引用，目标信息变化返回 `409 SOURCE_CHANGED`，拒绝时旧任务不撤销。相同命令已有回执时跳过预检，即使目标之后失效仍返回原结果；HTTP、Web 表单、批量与 WS 都走相同命令入口。预检通过不表示实际切换完成，仍按 operation 等待旧来源停止。

Python：`python -m pytest tests/integration/test_realtime.py`（使用项目环境）。
账号管理接口（已验收 P4 / rc7 / 030）：`GET /api/v1/accounts`（掩码列表）、`GET /accounts/{account_id}`、`POST /accounts/commands`、`GET /accounts/commands/{idempotency_key}` 与 `GET /accounts/operations/{operation_id}`，后三者均在 `/api/v1` 下。命令 action 为 create/rename/edit/test/enable/disable/delete/login-start/login-complete/login-cancel，非 create 必须提交 account_id、expected_revision，所有命令要求 idempotency_key。DB 配置命令与回执同事务；网络命令先持久受理，相同键不重复验证或发送验证码。UNKNOWN/PENDING 不表示成功，也不自动重放副作用，先核对账号状态再决定新的显式操作。

edit/disable/delete 可返回 202 和 operation_id；账号立即停止新选择，旧执行与远端停止确认后才完成，旧私有凭据版本在此期间保留。operation 完成时返回固定的掩码 `result`（删除为 account_id/deleted），后续其他客户端修改不改变该操作结果；客户端使用这次结果的 revision，不用另一次 GET 的最新版本悄悄覆盖草稿。列表另外返回按 account_id 映射的 `runtime`（state/error），启用意图与实际连接健康分别观察。login-complete/login-cancel 还要求 challenge_id；登录挑战仅进程内有效，旧版本或其他账号的挑战拒绝。HTTP Cookie 需要 X-CSRF-Token，Bearer 免 CSRF；读取/查看响应 no-store。`POST /api/v1/accounts/{account_id}/reveal` 明确查看配置，要求 expected_revision 与相同鉴权/CSRF，过滤 TG session。reveal 不在 WS 命令目录中，WS 只传掩码与非敏感回执。Web 复用这些 HTTP JSON 命令，WS 转发其余账号接口也进入同一核心。账号表单直接用 HTTP JSON 提交，实时表单转发不会重复接管；回复丢失后原输入重试复用原幂等键。P6/P7 再接同时监听和账号池，每类型当前只启用一个。
DOM 行为：在临时目录安装 `jsdom` 后运行 `node tests/web/realtime.cjs <jsdom 绝对路径>`。
账号表单另外运行 `node tests/web/accounts.cjs <jsdom 绝对路径>`，验证捕获阶段不重复提交、丢失回复重试、保留节点只绑定一次、CSRF 更新、掩码/草稿/登录挑战及停止完成后的固定版本结果。临时依赖和测试产物验证后清理。
该测试使用真实 HTMX/Alpine 和 WebUI 脚本，验证 Tag 左/右键、固定 URL、输入/勾选/焦点保留、按钮禁用、多值表单、断线不重发、HTTP 降级、序列/旧响应校正、日志快照及代理前缀。
DOM 测试与真实浏览器/移动设备联调分别记录，不能互相替代。


P5 身份/来源操作查询与重试（031 已冻结）：

- `GET /api/v1/ingest-operations/{operation_id}`：公开 ID 为 `reference:<uuid>` 或 `observation:<source_message_id>`。返回 `revision`、`kind`、`candidate_id`、`original_candidate_id`、`status`（PENDING/COMPLETED/FAILED）、`result_candidate_id`、`error_code`、`retryable`、`updated_at` 及统一中文 `state/label/error_message`。不返回消息原始 JSON、附件句柄、画廊 Token 或账号凭据。原候选合并/移除后仍可按操作 ID 查询；合并通知同时覆盖原作品与保留作品，原 Web 详情地址转向仍存在的保留作品。
- `GET /api/v1/works/{candidate_id}` 的 `ingest_operations` 使用同一投影，合并后的保留作品包含完成结果；PENDING 纳入 `live`。来源再次编辑会增加操作版本，不能把旧 revision 当作新输入重试。
- `POST /api/v1/ingest-operations/{operation_id}/retry`：JSON 为 `{"expected_revision":2,"idempotency_key":"client-generated-key"}`，成功受理返回 202，实际执行沿已有停止处理循环推进。只允许 FAILED 且身份/来源仍有效的操作；缺前置条件 428，陈旧版本/重复键异内容/已完成/来源变化 409，不存在 404。同 key 同内容在校验当前状态前返回原回执，Cookie/Bearer 共用管理员作用域；网络断开后应重发原 key，再 GET 最终状态。
- Cookie 写操作要求 `X-CSRF-Token`，Bearer 免 CSRF；成功查询/受理响应 `Cache-Control: no-store`。WS 命令 `api.api_ingest_operation` / `api.api_ingest_retry` 转发相同 JSON 路由；异步完成和失败沿已有事件通道通知客户端重读。Web 表单是同一命令的薄适配，不另做冲突裁决。
