# 实时 API：ehbot-realtime.v1（R86 / R87）

本契约已在服务端和 WebUI 实现。EhBot-Mobile 后续接入本协议，本轮不修改移动端代码。
判断能力应请求 `GET /api/v1/realtime` 与 `GET /api/v2/capabilities`，不能仅凭应用版本号判断。
此接口需认证，返回协议、带部署前缀的 WebSocket 路径、命令目录、epoch、sequence 和心跳间隔。

P10 已交付认证后的 `GET /api/v2/capabilities`；任务/作品控制和账号别名以 `v2.*` 命名进入同一动态目录，例如 `v2.downloads.cancel`、`v2.works.download`。使用目录公布的参数/路径，任务 UUID 为路径参数，JSON body 仍携带 expected_revision/idempotency_key；v1↔v2↔WS 共用原业务回执。HTTP 可用 Idempotency-Key 头，头体不一致409；WS用 body 字段。账号 reveal（含v2别名）继续只走HTTP，不进入目录或广播。协议仍为 ehbot-realtime.v1，无重放；command.accepted 不表示持久受理。

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

桌面端使用 `"client":"desktop"`，与移动端共用 JSON 命令。浏览器另加 `"csrf_token":"页面 token"` 和 `"client":"web"`。`logs:true` 同时订阅日志。
服务端回复：

```json
{"type":"ready","protocol":"ehbot-realtime.v1","epoch":"随机进程标识","sequence":12,"heartbeat_seconds":15,"replay":false,"commands":{"api.convert_work":{"method":"POST","path":"/api/v1/works/{candidate_id}/convert","params":["candidate_id"],"encoding":"json"}}}
```

实际 commands 包含全部已注册的有限 JSON 操作和快照；WebUI 还包括 `web.*` 表单/片段命令。
认证、流、缩略图和文件传输不作为命令，继续使用 HTTP。原生客户端可使用 `api.*` 与 `v2.*`，不提供 `web.*`。
命令目录来自明确注册的业务路由，无法传入任意 URL、方法、模块或函数。命令名属于 v1 契约，改名须同步契约及客户端。

## 提交操作与读取快照

```json
{"type":"command","request_id":"pack-42-1","name":"api.convert_work","payload":{"params":{"candidate_id":"42"},"body":{"work_id":"11111111-1111-4111-8111-111111111111","expected_revision":4,"idempotency_key":"pack-42-intent-1","input_artifact_id":"22222222-2222-4222-8222-222222222222"}}}
```

审核示例：

```json
{"type":"command","request_id":"review-1","name":"api.api_batch_review","payload":{"body":{"action":"approve","candidate_ids":[42,43],"idempotency_key":"review-intent-1","versions":{"42":{"work_id":"11111111-1111-4111-8111-111111111111","expected_revision":4},"43":{"work_id":"33333333-3333-4333-8333-333333333333","expected_revision":2}}}}}
```

元数据与队列快照示例：

```json
{"type":"command","request_id":"edit-42","name":"api.patch_metadata","payload":{"params":{"candidate_id":"42"},"body":{"work_id":"11111111-1111-4111-8111-111111111111","expected_revision":4,"idempotency_key":"edit-42-intent-1","fields":{"Series":"系列名称"}}}}
{"type":"command","request_id":"queue-1","name":"api.get_queue","payload":{"query":[["page_size","100"]]}}
```

`payload.params` 必须完整对应目录中的 params，值使用字符串；单个参数不可包含路径分隔符。
`body` 对 JSON 命令使用原 HTTP API 请求体；`query` 使用字符串二元组数组，支持重复筛选条件。
API 的字段、状态视图、批量限制、审核/删除/文件门禁及错误码与 HTTP 相同。
示例 UUID、版本和幂等键仅示意；实际使用刚读取的作品/产物身份和版本。同一操作重发保留原业务幂等键，每次传输使用新的 request_id。
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
连接关闭或命令协程被取消时，服务端等待该连接的子任务收束再释放连接资源；已经持久化受理的后台操作按其业务合同继续，重连后查询原回执或操作状态。
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

元数据命令契约：`api.patch_metadata`、`api.fetch_work_metadata` 的 JSON body 必须包含从编辑快照读取的 `work_id`、`expected_revision` 与本次 `idempotency_key`。HTTP 与 WS 重发共享同一回执；缺版本 428、旧版本 409、同键变内容 409。抓取返回持久 `metadata_fetch_operation_id`，运行中重发为 202；`GET /api/v1/metadata-fetches/{operation_id}` 只读状态，重启中断不自动重放网络请求。编辑器草稿保留原作品身份、版本和重试键；广播不替换这些值。`snapshot_changed=true` 时禁止自动按后来的快照重置草稿。原 HTML 元数据入口同样要求前置条件。

P10 工作区审核契约：`api.review_work`（POST /api/v1/works/{id}/review）以 `payload.params.candidate_id` 指定作品，body 包含 action（approve/reject/requeue）、work_id、expected_revision、idempotency_key，可带 note。旧 `api.requeue_work` 使用相同前置条件与回执；无论 HTTP/WS/普通表单均不代补当前版本。缺失 428，版本冲突或非法转换 409。单项审核回执包含原 work_id/revision、job_ids、task_ids；审核与任务意图原子提交。

`api.api_batch_review` 的 approve/reject/delete/delete-files body 包含 action、candidate_ids、批次 idempotency_key，以及 versions（以字符串 candidate_id 为键，值为 work_id/expected_revision）。批次及每项结果持久固定，重发返回原 applied/skipped；中断后继续尚未完成项，不能按后来状态将原成功重算为跳过。同键不同选择/顺序/动作/版本 409。原 Web 批量表单带每行所见版本，同样调用共享命令。批量冲突保留原选择/版本/key，实时广播不重置；管理员主动筛选才释放旧选择。

`api.delete_work` 对应 `POST /api/v1/works/{id}/delete`：params.candidate_id 指定作品，body 包含 work_id、expected_revision、idempotency_key，可带 mode=delete/delete-files。HTTP/WS/旧 Web 删除共用回执；202 的 removal_id、operation_ids、cleanup_status=PENDING 是固定受理结果。使用 `api.api_removal`（GET /api/v1/removals/{removal_id}）读实时清理状态，不能用重放回执推断文件已删。旧数字编号被重用也不会把旧命令作用到新作品；缺版本428、过期版本409。原表单冲突保留删除模式和前置条件，HTMX 显示409/428错误。

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

P6 / 工作区 035：TG 可接替的下载失败登记内部 `replace-account` operation；STOPPING 仍占槽，原请求退出后，合格备用恢复同一 task 的 PENDING，下次领取生成独立 attempt。有限账号池与已尝试集合跨重启保留，旧失败 attempt 不变。此时管理员仍可用现有版本/幂等任务 `cancel` 命令取消接替；返回 STOPPING 与原 operation_id，最终 CANCELLED，预检期间取消也不会启动后继。其他 STOPPING 操作仍遵循原限制。账号生命周期触发接替和显式重试后的账号重选尚待 P6 完整验收。

STOPPING 保留作品槽。已验收 P3 / schema 029 接入本地请求/线程/进程树收束与远端推送查回/归属核实；确认停止后才完成 operation 并入队切源/重试目标。推送结果未知、失联或实例/归属改变时 operation 保持未完成并记录 error_code；新共享种子拒绝附着，轮询核对原实例和本 attempt 标签后才写进度/产物。运行移除立即删除业务行，返回 `removal_id`、`operation_ids`、`cleanup_status`，独立收尾继续保留。`GET /api/v1/removals/{removal_id}` 复用 Cookie/Bearer 鉴权，返回状态、关联任务、停止错误与 deleted_files/failed_files；业务候选已删除时仍可查。任务详情新增 `removals`，其 PENDING 表示独立作品预留尚未释放，不能用任务的 REMOVED 推断下载已停止。默认保留文件，同作品新任务排队等待停止和文件清理确认。完整发布崩溃恢复、心跳/预算按 P8/P9 接续。旧客户端缺少前置条件直接拒绝，不自动采用服务器最新版本。

`switch-source` 先读取管理员作用域回执，再在数据库写事务外验证目标凭据/链接与页面可读性；不下载目标原档或图片。最终受理事务再次核对 task/revision 和目标引用，目标信息变化返回 `409 SOURCE_CHANGED`，拒绝时旧任务不撤销。相同命令已有回执时跳过预检，即使目标之后失效仍返回原结果；HTTP、Web 表单、批量与 WS 都走相同命令入口。预检通过不表示实际切换完成，仍按 operation 等待旧来源停止。

Python：`python -m pytest tests/integration/test_realtime.py`（使用项目环境）。
账号管理接口（已验收 P4 / rc7 / 030）：`GET /api/v1/accounts`（掩码列表）、`GET /accounts/{account_id}`、`POST /accounts/commands`、`GET /accounts/commands/{idempotency_key}` 与 `GET /accounts/operations/{operation_id}`，后三者均在 `/api/v1` 下。命令 action 为 create/rename/edit/test/enable/disable/delete/login-start/login-complete/login-cancel，非 create 必须提交 account_id、expected_revision，所有命令要求 idempotency_key。DB 配置命令与回执同事务；网络命令先持久受理，相同键不重复验证或发送验证码。UNKNOWN/PENDING 不表示成功，也不自动重放副作用，先核对账号状态再决定新的显式操作。

edit/disable/delete 可返回 202 和 operation_id；账号立即停止新选择，旧执行与远端停止确认后才完成，旧私有凭据版本在此期间保留。operation 完成时返回固定的掩码 `result`（删除为 account_id/deleted），后续其他客户端修改不改变该操作结果；客户端使用这次结果的 revision，不用另一次 GET 的最新版本悄悄覆盖草稿。列表另外返回按 account_id 映射的 `runtime`（state/error），启用意图与实际连接健康分别观察。login-complete/login-cancel 还要求 challenge_id；登录挑战仅进程内有效，旧版本或其他账号的挑战拒绝。HTTP Cookie 需要 X-CSRF-Token，Bearer 免 CSRF；读取/查看响应 no-store。`POST /api/v1/accounts/{account_id}/reveal` 明确查看配置，要求 expected_revision 与相同鉴权/CSRF，过滤 TG session。reveal 不在 WS 命令目录中，WS 只传掩码与非敏感回执。Web 复用这些 HTTP JSON 命令，WS 转发其余账号接口也进入同一核心。账号表单直接用 HTTP JSON 提交，实时表单转发不会重复接管；回复丢失后原输入重试复用原幂等键。P6/P7 已接多 TG 同时监听与 EH 有限账号池，各类型均可启用多个账号。
DOM 行为：在临时目录安装 `jsdom` 后运行 `node tests/web/realtime.cjs <jsdom 绝对路径>`。
账号表单另外运行 `node tests/web/accounts.cjs <jsdom 绝对路径>`，验证捕获阶段不重复提交、丢失回复重试、保留节点只绑定一次、CSRF 更新、掩码/草稿/登录挑战及停止完成后的固定版本结果。临时依赖和测试产物验证后清理。
该测试使用真实 HTMX/Alpine 和 WebUI 脚本，验证 Tag 左/右键、固定 URL、输入/勾选/焦点保留、按钮禁用、多值表单、断线不重发、HTTP 降级、序列/旧响应校正、日志快照及代理前缀。
DOM 测试与真实浏览器/移动设备联调分别记录，不能互相替代。

P5 身份/来源操作查询与重试（031 已冻结，已提交 2ea49f2）：

- `GET /api/v1/ingest-operations/{operation_id}`：公开 ID 为 `reference:<uuid>` 或 `observation:<source_message_id>`。返回 `revision`、`kind`、`candidate_id`、`original_candidate_id`、`status`（PENDING/COMPLETED/FAILED）、`result_candidate_id`、`error_code`、`retryable`、`updated_at` 及统一中文 `state/label/error_message`。不返回消息原始 JSON、附件句柄、画廊 Token 或账号凭据。原候选合并/移除后仍可按操作 ID 查询；合并通知同时覆盖原作品与保留作品，原 Web 详情地址转向仍存在的保留作品。
- `GET /api/v1/works/{candidate_id}` 的 `ingest_operations` 使用同一投影，合并后的保留作品包含完成结果；PENDING 纳入 `live`。来源再次编辑会增加操作版本，不能把旧 revision 当作新输入重试。
- `POST /api/v1/ingest-operations/{operation_id}/retry`：JSON 为 `{"expected_revision":2,"idempotency_key":"client-generated-key"}`，成功受理返回 202，实际执行沿已有停止处理循环推进。只允许 FAILED 且身份/来源仍有效的操作；缺前置条件 428，陈旧版本/重复键异内容/已完成/来源变化 409，不存在 404。同 key 同内容在校验当前状态前返回原回执，Cookie/Bearer 共用管理员作用域；网络断开后应重发原 key，再 GET 最终状态。
- Cookie 写操作要求 `X-CSRF-Token`，Bearer 免 CSRF；成功查询/受理响应 `Cache-Control: no-store`。WS 命令 `api.api_ingest_operation` / `api.api_ingest_retry` 转发相同 JSON 路由；异步完成和失败沿已有事件通道通知客户端重读。Web 表单是同一命令的薄适配，不另做冲突裁决。

P6 工作区新增来源账号选择与会话读取（P6 已验收）：

- `GET /api/v1/settings/sources` 的每条来源增加 `account_id`、`account_label`、`account_kind`、`revision`；历史账号未知时账号字段为 null。`source_accounts` 列出可配置的 TG Bot/用户，字段仅 account_id/kind/label/enabled，不含凭据或会话。不同账号的相同 Chat ID 分别列出。`source_versions` 列出 source_id/account_id/chat_id/revision/dismissed，含已隐藏的墓碑，供明确重新添加使用。
- `POST /api/v1/settings/sources/dialogs` 请求 `{"account_id":"所选 TG 用户 UUID"}`，返回 `{"items":[...],"total":1}`。每条会话保留该 account_id，并沿用 chat_id/title/kind/username 字段。只读会话、不修改来源；账号不存在 404，类型不支持、停用或有歧义 400。省略账号仅兼容恰好一个已启用且连接正常的 TG 用户，多账号不静默取第一项。
- Cookie 请求需要 `X-CSRF-Token`，Bearer 免 CSRF；WS `api.api_source_dialogs` 以 payload.body 转发同一接口。Web 来源页调用同一账号服务，预填与批量添加携带选定账号，更新来源保留账号归属。
- P6 已支持多个 TG Bot/用户同时启用并完成来源/监听/下载接替验收；P7 已接 EH 多启用和有限池，全域协议核对在 P10。

P6 来源命令（032 已冻结；Cookie/CSRF、Bearer 和 WS 共用同一核心）：

- `POST /api/v1/settings/sources`：原身份/规则字段加 `expected_revision`、`idempotency_key`。新建版本为 0；更新或恢复墓碑必须提交该行已读取的版本。版本冲突 409 `REVISION_CONFLICT`，details.current 提供非敏感当前配置；缺版本/幂等键返回 428。原 `/sources` 表单不再无条件 upsert。
- `PUT /api/v1/settings/sources/batch`：action/source_ids/规则字段加 `expected_revisions`（source_id 字符串到版本的映射）及 `idempotency_key`。逐项裁决，冲突项不影响其他项；不将已隐藏行悄悄恢复。
- `POST /api/v1/settings/sources/batch-add`：`account_id`、`idempotency_key` 和 `entries` 数组；每项含 source_type/chat_id/display_name/expected_revision。不存在用 0，已存在须用其读取版本；可见行返回 SKIPPED 并保留配置，墓碑仅在版本匹配时恢复为停用。
- 写响应继续包含当前来源快照，另含稳定的 `command`：command_id/idempotency_key/action/status/items；status 为 COMPLETED/PARTIAL/FAILED，逐项返回 source_id/chat_id/revision/status/created 或 error。批量业务失败以逐项结果返回；格式/前置条件错误仍是错误信封。当前快照可能已更新，重放保证的是同一 command 回执。
- `GET /api/v1/settings/sources/commands/{key}` 可在断线后取回回执。相同管理员跨 Cookie/Bearer/WS 复用同键同内容得到同一回执，校验顺序先回执后当前版本；同键异内容 409 `IDEMPOTENCY_CONFLICT`。来源变更与整批回执在同一短事务提交；异常全部回滚。
- 自动 WS 目录包含 api.post_source/api.put_sources_batch/api.api_add_sources_bulk/api.api_source_command_receipt；使用 payload.body 或 payload.params.key。Web 旧表单也要求原版本和幂等键，冲突不自动改版本重试。消息游标不推进配置 revision，不同来源互不制造版本冲突。
- 033 已冻结：原单账号凭据导入后的来源规则一次性复制给对应 legacy_slot 的 TG_BOT/TG_USER，已有专属配置保留；完成标记与复制同事务提交，后续账号不自动继承，重启不覆盖编辑/删除。原共享配置保留历史引用并从 sources/source_versions 隐藏，历史 observation 身份不改。旧共享保存缺明确账号返回 ACCOUNT_SELECTION_REQUIRED；退休来源批量写按旧版本拒绝，已读到退休版本也返回 SOURCE_MIGRATED。客户端应重新读取来源并选择账号。
- 034 已冻结：TG 用户接收消息和来源游标同事务持久化，候选消费从网络轮询分离；没有新增客户端命令。账号运行状态继续报告消费失败，接收不会因此停止；服务器恢复后继续消费，不要求客户端重发入库操作。FloodWait 仅暂停对应用户账号的监听请求，完整持久冷却/跨功能预算在 P9。来源配置 revision 不随接收游标变化。


P7 / 036 已冻结：归档/认证种子复用任务命令和操作查询，不新增平行下载接口。task.attempts 的 details_json 保留 account_id、credential_revision、有限账号池；原始错误保留在各次 attempt。GP/认证明确失败可生成新的 attempt；共享限流不轮换。费用结果不明返回 EH_ARCHIVE_RESULT_UNKNOWN，普通 retry/restart 只读核实原申请，不能以新账号绕过未知费用。收费意图的公开字段不包含 Cookie 或私有定位 URL；eh_archive_intent_id 只是不可推导的定位标识。

P8 / 037 的精确打包输入在 P10 补齐命令合同：`api.convert_work`（POST /api/v1/works/{id}/convert）和 `api.pull_work_source`（POST /api/v1/works/{id}/sources/{provider}）的 body 必须包含 work_id、expected_revision、idempotency_key；原 Web 来源/打包表单同样校验。缺失 428、版本冲突 409；网络准备后再做短事务 CAS，任务与回执一起提交。相同键跨 HTTP/WS 重发只返回原任务，即使它后来完成/失败/取消，也不重新入队；明确重做需新版本和新键。响应 revision/task_revision 属于任务，work_revision 属于作品。

convert 可另带 `input_artifact_id` 固定原档；未指定时选择当前已完成的原档。ID 必须是非空字符串并属于该作品的原档；新下载尚未完成且未明确选择旧版时返回 `ARCHIVE_NOT_READY`，已有不同输入/不同类型的活动打包返回 `PACK_INPUT_CONFLICT`。显式输入保存在任务与 attempt 的 details_json，重试不临时选择另一版。空请求体/`{}` 不再满足 P10 前置条件。

`GET /api/v1/tasks/{task_id}` 增加 `pack_intent`（pack_requested、input_artifact_id、pack_task_id、status），`artifacts`（版本 ID、类型、生产 attempt、路径/摘要/大小/页数），以及安全摘要 `publications` 和 `relocations`。打包意图状态为 WAITING/READY/QUEUED/CANCELLED；发布和移动状态为 PREPARED/COMMITTED/ROLLED_BACK/RECOVERY_REQUIRED。历史产物的 producer_attempt_id 为 null，不伪造生产者；移动摘要不含内部恢复副本路径。`PUBLICATION_RECOVERY_REQUIRED` 需先核实文件，不自动覆盖或清理未知内容。下载完成与打包通知仍复用现有事件，持久意图负责补偿丢失通知。

P9 / 038（本地定向验收通过，未发布）：attempt 的 lease_owner 为后台 session UUID；心跳续约不推进任务编辑 revision，旧 session 的完成写入被拒绝。历史 attempt 的 INTERRUPTED / WORKER_SESSION_LOST 表示进程或派发失联，自动恢复最多 3 次；WORKER_RECOVERY_EXHAUSTED 需核对原因后显式重试。停止和远端确认仍复用 operation，不因后台重启返回虚假的完成状态。TG 账号 runtime health 在冷却中返回 error 状态、固定脱敏说明、error_code 和 retry_at（UTC）；凭据与会话不进入该响应。客户端按权威快照刷新，不能用定时进度覆盖编辑草稿。

P10 已下载域：`api.downloaded_batch` 的 body 包含 action/candidate_ids/批次 idempotency_key/逐项 versions；每项提交 work_id/expected_revision。重发返回固定原任务或移除受理，不能复活后来终态任务。`api.rename_downloaded` 与 `api.set_work_archive_path` 同样要求所见身份/版本/key，并接受 filename/directory/布尔repack；路径移动、pin、可选打包与回执共用P8恢复日志/最终事务。旧表单与HTTP↔WS共享结果；409保留草稿/原版本/key，广播不替换基础版本。

`api.downloaded_dedup_plan` 返回固定预览plan_id，`api.downloaded_dedup` 的body必须提交plan_id/idempotency_key。确认不重算当前库，删除事务核对保留项和待删项，逐项成功/拒绝固定；中断重发继续未完成项，后来出现的重复不加入旧计划。运行清理状态仍查GET /removals/{id}。

手动新增 `POST /api/v1/candidates/manual`（WS使用命令目录中的同一路由）提交 url/idempotency_key，缺key为428，同键不同链接为409；同键重放先读回执，不再抓取或检查当前连接。RUNNING返回202，其余成功查询返回200；GET /api/v1/manual-add-operations/{operation_id} 返回固定 COMPLETED/FAILED/UNKNOWN 或当前 RUNNING，均 no-store，不含原链接或凭据。COMPLETED含原 candidate_id/work_id/revision；删除重加后仍为原回执，客户端须核对稳定身份。FAILED重放原错误，UNKNOWN需核实而不自动重发。旧表单也保留原链接/key；Web操作查询检查身份，原作品已删除返回410。

设置命令：GET /api/v1/settings/{section} 的 settings_versions 提供各保存区版本；数据读取前后核对版本，连续三次变化返回409 SETTINGS_BUSY。以下写入口需body中的整数expected_revision/idempotency_key，缺失428、旧版本409、同键不同命令409；成功返回固定{scope, revision, status: COMPLETED}，界面数据另行GET，不再随重放返回后来的快照。WS命令从同一路由目录取得，旧表单走同一核心。

| 写入口 | section / scope |
|---|---|
| PUT /api/v1/settings/system | system / system |
| PUT /api/v1/settings/parse、POST /api/v1/settings/parse/reset | parse / parse |
| PUT /api/v1/settings/parse/ai | parse / admission |
| PUT /api/v1/settings/archive/limits | archive / archive-limits |
| PUT /api/v1/settings/archive/torrent | archive / torrent |
| PUT /api/v1/settings/archive/auto-pack | archive / auto-pack |
| PUT /api/v1/settings/paths/template | paths / template |
| PUT /api/v1/settings/paths/directories | paths / directories |
| PUT /api/v1/settings/ai/master | ai / ai-master |
| PUT /api/v1/settings/paths/ai | paths / path-ai |
| PUT /api/v1/settings/paths/ai/prompt | paths / path-ai |
| PUT /api/v1/settings/paths/ai/models | paths / path-model-source |
| PUT /api/v1/settings/parse/model-source | parse / candidate-model-source |

同区修改、版本与安全回执同事务，路径可写预检在锁外并在提交时再CAS；接受无变化的保存也消费旧版本。运行状态或无关维护字段不推进这些编辑版本。系统运行时缓存提交后按序重读；重复回执也可补偿遗漏刷新。表单409/428保留原草稿/基础版本/key，后台广播保留原DOM，显式确认采用当前版本会生成新的可重放命令。密码库/API Key合同见下。

`PUT /api/v1/settings/passwords` 的四个 action 共用 HTTP/WS/旧表单命令核心。GET 快照包含 `settings_versions.vault`、`settings_versions.api-key`；add（按名称新增或更新）使用 vault 集合版本，generate/revoke 使用 api-key 版本。delete 提交 `password_id/password_uuid/expected_revision`（条目版本），编号复用或原对象删除返回410。所有动作提交独立 `idempotency_key`，缺条件428、旧版本409；写入/版本/安全回执同事务，认证 touch 与解压成功时间不推进编辑版本。

成功返回固定安全回执 `action/scope/status/revision`，密码操作另含条目身份/版本，生成另含 `public_id`。仅首次成功生成的响应附 `api_key_plaintext`，不进入持久回执或广播；同键重放只返回安全回执，不重新生成。断线后以原键重发查回执，再GET当前配置；明确替换必须使用新键和当前版本。原生页面直接200显示首次结果，不再使用跳转后的明文读取槽。密码派生 Cookie/access token 可管理 Key；API Key 自身调用生成/撤销返回403 `PASSWORD_LOGIN_REQUIRED`。响应均 no-store。

路径 AI 大表单提交 `path_source/ai_prompt/ai_batch_size/ai_concurrency/ai_fallback_to_rules/ai_stream/ai_default_include_current/auto_series`，数字必须合法，开关省略为 false；全部校验后一次事务保存。独立 prompt 命令提交 `prompt`，空字符串恢复随版本更新的默认提示词，与大表单共用 `path-ai` 版本，使用独立幂等键。两种模型来源命令提交 `model_source`（`default` 或 `custom`），各自独立版本；旧表单 `ai_model_source` 与 JSON 归一。开启 `auto_series` 时在最终写事务内复查总开关和当前选择的有效链，前置条件已变化则返回 `SERIES_AI_UNAVAILABLE`，不保存任何面板字段。命令仅返回固定 `scope/revision/status`，当前设置另 GET；HTTP/WS/原表单同键重发共享回执。

qBittorrent 命令的 JSON 未提交字段保持原值，密码空值与省略均保留已保存密码，拒绝把 `********` 当成新密码。敏感命令内容以私有主密钥的 HMAC 裁决，普通回执只有 scope/revision/status，不保存密码或可离线猜测的裸摘要。原表单将未勾选开关显式转换为 false；跨通道重发须使用同一组已规范化字段。原实例与 attempt 的远端归属保护独立于配置编辑版本，保存配置不会绕过停止确认。

`GET /api/v1/settings/archive` 中的 profiles 各自返回 profile_uuid/revision。`PUT /api/v1/settings/archive/profiles/{name}` 修改已登记的工具，需 profile_uuid、整数expected_revision及idempotency_key，可部分提交enabled/executable_path/timeout_seconds。启停与字段保存共用逐条版本；缺条件428，过期409，原配置删除或名称/编号复用410；成功只返回固定{name, profile_uuid, revision, status: COMPLETED}，当前值另GET。原 `/archive-settings/profiles/{name}` 表单使用同一处理器，不补最新版本；WS从相同路由目录取得命令。

审批/路径规则共用同一命令核心。POST `/api/v1/settings/auto-approval/rules`、`/api/v1/settings/paths/rules`，以及其 `/{rule_id}/toggle` PUT 和 `/{rule_id}` DELETE，在HTTP和WS均提交JSON body。新增要求 `expected_revision=0/idempotency_key`；已有规则要求列表所读 `rule_uuid/expected_revision/idempotency_key`，编辑沿用 `rule_id` 或 `path_rule_id`。成功返回固定 `domain/rule_id/rule_uuid/revision/action/status`，列表另GET。缺条件428，版本过期409，原规则删除或数字编号复用410；先读持久回执再查当前版本，同键重发不重新执行。各条规则独立版本，启停也推进版本；试跑不写规则、不消耗版本。旧表单复用相同核心并保留条件草稿、原版本与键，显式核对新版本才产生新命令。

路径维护提供以下共享 JSON/WS 入口（原 Web 表单复用相同命令）：

作品手动来源修改使用 PUT `/api/v1/works/{id}/eh-ref`、`/preview`、`/torrent`，各带 `work_id/expected_revision/idempotency_key` 及 `ex_gid`（完整画廊URL）、`preview_url` 或 `magnet`。WS使用动态目录中的同一路由，原表单复用相同SourceCommands。已有画廊合并另需目标 `target_work_id/target_revision`，冲突409 `MERGE_TARGET_CHANGED` 返回安全目标身份供明确确认；等待旧下载停止期间目标变化则操作失败 `INGEST_REFERENCE_TARGET_CHANGED`，不能重试旧合并意图。返回 `operation_id` 时通过原摄取操作查询，原键重放不重新修改来源；缺条件428、旧版本409。

| 路由 | 条件与结果 |
|---|---|
| POST `/api/v1/settings/paths/rearchive` | `expected_revision=settings_versions.rearchive`、`idempotency_key`；可选布尔 `force/dry_run`，默认 false；返回固定 `scanned/force/dry_run/queued/refiling/moved/unchanged/skipped` |
| GET `/api/v1/settings/paths/rearchive?idempotency_key=...` | 只读回执：`status=INCOMPLETE/COMPLETED`、`requested/completed/outcomes/result`；无回执404，查询不继续执行 |
| POST `/api/v1/settings/paths/ai/cache/clear` | `expected_revision=settings_versions['path-cache']`、`idempotency_key`；返回固定设置回执及 `removed`，旧回执不会再次清除新答案 |
| POST `/api/v1/settings/paths/rules/dry-run` | 规则 AST `condition`、可选 `case_sensitive`；返回 `dry_run`，与 Web 复用原校验和试跑服务，不写规则 |
| POST `/api/v1/settings/paths/template/preview` | `library_template/title_source`；返回 `template_preview`，与 Web 使用同一样例与文件系统限制，不保存设置 |

重新归档先以全局范围版本检查规划前后的一致性，再固定各作品 UUID/revision、精确输入 artifact 和路径策略。规划只读本地数据，不调用 EH 或模型；每个子项提交再次核对原作品、配置和 AI 答案，失败逐件记录，已完成子项优先回放。移动复用文件日志，入队/pin/安全回执同事务；运行中断后可明确以原版本/键/内容重发继续。临时文件移动阻塞409 `PUBLICATION_RECOVERY_REQUIRED` 不冻结为永久子项失败。整批完成后重放始终返回原最终报告；回执查询不返回内部规划或供应商载荷。试跑也保存固定报告，但没有执行子项（`completed=0`）。写入缺条件428、过期409；所有维护回执响应 no-store。WS 命令名从动态目录读取。

手动新增的 `CANDIDATE_DUPLICATE` 回执在判重事务中固定 `existing_candidate_id/existing_work_id`，重发不查询后来复用该编号的作品。旧回执缺少稳定身份时不能推定其属于当前编号。Web重复提示和成功跳转携带 `?work_id=`，详情页在打开时再次核对；原作品删除返回410。profile原生保存错误页即使已无该记录也保留原草稿/身份/版本/key，不允许确认采用同名新记录的身份。

工具安装从 `GET /api/v1/settings/archive` 的 `toolchain.revision` 读取所见版本。`POST /api/v1/settings/archive/toolchain/install` 提交整数 `expected_revision`、`idempotency_key` 与可选布尔 `force`（默认true）。受理/版本递增同事务，返回202固定 `{toolchain_operation_id, status: ACCEPTED, revision}`；同键同内容重发仍返回原受理，缺条件428，旧版本/键冲突/另一个安装在执行均409。profile编辑版本与安装版本互不影响，运行健康和操作完成不推进安装版本。

AI总开关从 `GET /api/v1/settings/ai` 的 `settings_versions['ai-master']` 读取版本，PUT提交 `enabled/expected_revision/idempotency_key`；返回固定 `scope/revision/status`，当前开关值另GET。复用SettingsCommands的原子提交与安全回执，原内部save_ai_enabled变化也推进该区版本，其他功能设置和健康用量不推进它。旧表单继续使用ai_enabled复选框，省略为false，与JSON的enabled=false归一；冲突不自动换版本，草稿明确确认才形成新命令。

AI供应商的POST `/api/v1/settings/ai/providers` 返回固定安全回执，当前值另GET。新增使用
`settings_versions['ai-providers']` 作为 `expected_revision`；编辑另提交所读 `provider_id/provider_uuid`，
`expected_revision` 使用该供应商自己的 `revision`。两者均需 `idempotency_key`。DELETE
`/api/v1/settings/ai/providers/{provider_id}` 的JSON body还必须带所读 `keys_revision/models_revision`，
防止连带删除新子配置。缺条件428，过期409，原UUID不存在或数字ID复用410。同键重放先查回执，
配置/版本/回执同事务；回执只含action、provider_id、provider_uuid、revision、keys_revision、models_revision、
collection_revision及status，不包含地址、请求头、参数或秘密。摘要使用私有主密钥HMAC。

原Web表单和WS复用供应商命令，字段归一后可跨通道重放；冲突保留原草稿，明确确认才用当前版本与新键。
原供应商已删除时不能确认采用新对象身份。保存跳转附带provider_uuid，旧回执链接不会选择复用编号的新对象。
供应商/Key/模型均投影稳定UUID及独立revision；供应商另有子配置版本，健康/用量/冷却/验证不推进编辑版本。
`ai-catalogue`仅用于快照前后核对，不能用作其他保存区的编辑版本。验证写回核对原供应商与凭据配置版本、
模型UUID/版本；变化时返回AI_CONFIGURATION_CHANGED，跳过写回。配置变更清除旧验证标记，迟到Key健康结果也不能写复用编号。
Key/模型/链及路径AI参数/模型来源公开编辑和验证/拉取模型持久操作已接通。

AI 网络操作使用下列 POST 路由（前缀 `/api/v1/settings/ai`），HTTP 与 WS 使用同一核心：

| 路由 | body 中所见条件（均另需 idempotency_key） |
|---|---|
| `/models/{model_id}/verify` | model_uuid / expected_revision=模型revision / provider_uuid / provider_revision / keys_revision |
| `/providers/{provider_id}/verify` | provider_uuid / expected_revision=供应商revision / keys_revision / models_revision |
| `/providers/{provider_id}/models/fetch` | provider_uuid / expected_revision=供应商revision / keys_revision |
| `/verify` | expected_revision=settings_versions['ai-catalogue'] |

条件版本为整数。缺条件428、配置过期409、原对象移除或编号复用410；同一模型的单项和批量测试互斥，重叠返回409 `AI_NETWORK_BUSY`。
不同模型可并发，拉取仅对同一供应商的其他拉取互斥。操作不消耗配置编辑版本；全局测试固定目录中所有启用模型，包括停用供应商下的启用模型。
受理事务固定原供应商/模型配置及原Key信封，随后锁外网络；信封、地址、请求头和参数不写入普通操作表或回执。
同键相同规范内容始终返回202 `{ai_operation_id, status: 'ACCEPTED', result_url}`，即使原对象已删除；同键不同内容409。
`GET /api/v1/settings/ai/operations/{operation_id}` 鉴权后返回 `action/target/status/result/error_code`，状态为 RUNNING、COMPLETED、FAILED、UNKNOWN；受理及查询均no-store。
拉取结果为 `{names:[...]}`，测试结果为 `{entries:[{model_uuid,provider_uuid,ok,code}],ok,total}`；批量中各模型失败在entries表示，单模型失败状态为FAILED。
结果只含安全code，不包含供应商原始错误文本。配置变化时迟到测试返回 `AI_CONFIGURATION_CHANGED`，不更新当前模型的验证结果。
客户端断线不取消已受理执行；重启将原RUNNING标UNKNOWN、释放探测claim，不自动重发。旧表单受理303跳结果页；最近10项在AI快照的network_operations中提供，GET及刷新不会重发。

从拉取清单添加模型时，`POST /providers/{id}/models` 在原models-add条件外另带 `discovery_operation_id`。
保存事务要求它仍为原供应商配置/Key版本的已完成fetch，否则409 `AI_DISCOVERY_STALE`，不添加模型或消费集合版本。
原表单自动带此字段，错误页保留原操作与选择，不确认采用已过期的清单；需要重新拉取。成功回执仍优先重放，不因后来的配置变化失效。

Key/模型命令同样从 `/api/v1/settings/ai` 读取条件：

| 写入口（均在 `/api/v1/settings/ai` 下） | 身份与 expected_revision |
|---|---|
| POST `/providers/{id}/keys` | provider_uuid / keys_revision |
| POST `/providers/{id}/models` | provider_uuid / models_revision |
| PUT `/keys/{id}/toggle`；POST `/keys/{id}/reset`；DELETE `/keys/{id}` | key_uuid / Key 的 revision |
| PUT `/models/{id}/toggle`、`/models/{id}/params`；DELETE `/models/{id}` | model_uuid / 模型的 revision |

每项都需 idempotency_key；reset 还提交所读整数 expected_health_revision。健康变化返回409
HEALTH_REVISION_CONFLICT，不能用旧页面清掉新产生的冷却。reset只推进健康版本；其他编辑与接受无变化的操作
消费配置版本。成功回执仅含安全身份、版本和动作，新增返回 entries 身份列表；不回传 Key、密文或参数。
同键跨HTTP/WS/原表单重放返回原结果，重放reset不会再次清掉后来出现的冷却。缺条件428、过期409、原身份不存在410。
冲突页保留原身份/版本/key与非敏感草稿；服务器不回显 api_keys，实时页面仅通过DOM属性保留当前输入。
无JavaScript或重新打开页面时须重新粘贴Key。原对象删除后草稿仍可查看，不接受同编号新UUID。

PUT `/api/v1/settings/ai/chain/{scope}` 的scope为 default、archive_path、candidate_admission。
从对应ai/paths/parse设置快照的 `settings_versions['ai-chain:' + scope]` 读取expected_revision，
提交model_id/model_uuid/action/idempotency_key；action为primary、append、shift、remove，shift另传整数delta
（负数上移、正数下移，超出范围移到端点）。CAS、原排序规则、整链保存与固定安全回执同事务；
返回scope/action/model_id/model_uuid/revision/status，列表另GET。不同scope独立，健康与模型参数不推进排序版本，
内部保存和模型删除导致的列表变化会推进它。原模型身份失效410，排序过期409；先查回执，已成功原操作重放不再次排序。
旧页面表单提交同一核心；下拉框的model_identity携带编号与UUID，冲突确认只更换所见链版本和操作键，不更换模型身份。

`GET /api/v1/settings/archive/toolchain/operations/{operation_id}` 返回当前 `RUNNING/COMPLETED/FAILED/UNKNOWN`、受理revision、当时安装目标version及安全error_code，均no-store。HTTP与WS使用同一路由目录，原安装表单复用同一处理器并保留冲突版本/key，明确核对后才能新建命令。断线不会取消已受理安装；优雅停机等待真实线程退出。重启将未完成操作记为UNKNOWN，不自动重发，启动自动安装也等待管理员核对；需要新安装时用新键和当前版本明确受理。实际文件操作另有跨线程/进程的目录锁，保护CLI、启动及Web安装；普通操作回执不包含路径、下载内容或凭据。
