# 实时 API：ehbot-realtime.v1（R86）

本契约已在服务端和 WebUI 实现。EhBot-Mobile 后续接入本协议，本轮不修改移动端代码。
服务端版本号仍为 `0.3.0rc3`，判断能力应请求 `GET /api/v1/realtime`，不能仅凭版本号判断。
此接口需认证，返回协议、带部署前缀的 WebSocket 路径、命令目录、epoch、sequence 和心跳间隔。

## 连接与认证

- 路径：`/ws/ui`；有 `APP_ROOT_PATH=/ehbot` 时是 `/ehbot/ws/ui`。HTTP 对应 `ws`，HTTPS 对应 `wss`，与服务器使用相同主机/端口。
- WebUI：已有 Session Cookie，同源 `Origin`，hello 携带当前页面 CSRF token。
- 原生客户端：握手请求头 `Authorization: Bearer <access token 或 API Key>`，无需 Cookie/CSRF/Origin。不把凭据放在 URL、帧、日志或本地可分享的连接配置中。
- refresh token 仅用于 HTTP 刷新接口，不能用于此连接。保留 `/api/v1/auth/login|refresh|logout|whoami`。
- 认证失败在升级前关闭；协议/CSRF 错误在升级后返回 `error` 并关闭。活跃连接每条命令及心跳重新验证凭据；到期/撤销返回现有认证错误码并关闭。Cookie 会话受原有 14 天期限及退出/改密约束。

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

HTTPS 由代理终止时，还需按现有可信代理配置传递并信任转发协议/主机，保持 Origin 同源。
不要给 WS 另造不匹配的域名，或把 API Key 拼进 proxy_pass/访问日志。

## 验证命令

Python：`python -m pytest tests/integration/test_realtime.py`（使用项目环境）。
DOM 行为：在临时目录安装 `jsdom` 后运行 `node tests/web/realtime.cjs <jsdom 绝对路径>`。
该测试使用真实 HTMX/Alpine 和 WebUI 脚本，验证 Tag 左/右键、固定 URL、输入/勾选/焦点保留、按钮禁用、多值表单、断线不重发、HTTP 降级、序列/旧响应校正、日志快照及代理前缀。
DOM 测试与真实浏览器/移动设备联调分别记录，不能互相替代。
