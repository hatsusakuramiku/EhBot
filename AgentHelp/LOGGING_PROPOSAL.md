# 日志管理方案（提案）

> **状态**：L1 已于 2026-08-29 实施（commit v0.2.3）。L2、L3 仍未实施。（「取 github 找找比较好用的日志管理方案，先写个方案看看」）写成。
> **调研数据已联网核对**（同日）。初稿曾写「无外网」，那是错的：
> 沙箱**默认**拦网络，提权后可正常访问 GitHub 与 PyPI。
> 本文第 3 节的 star、发布时间、依赖清单与 `loguru` 的 `diagnose` 默认值
> 均为实测所得，采集于 2026-08-29。

## 1. 现状：不是「没有日志」，而是「有一个薄壳且漏三样东西」

先纠正一下问题的前提。`app/logging.py` 已经存在，并且做对了两件不容易的事：
输出是**结构化 JSON**，并且**在格式化阶段脱敏**（Telegram bot token、Authorization、
Cookie、URL query、`ipb_pass_hash` 等）。`app/main.py:56` 在 `create_app()` 里调用
`configure_logging()`，全仓 60 余处 `logging.getLogger(__name__)` 调用点都走它。

所以这不是从零开始，而是把一个薄壳补成能运维的东西。下面每一条都在本机复现过。

### 1.1 致命：异常堆栈被静默丢弃

`JsonFormatter.format()` 只读 `record.getMessage()`，**从不读 `record.exc_info`**。
于是四个 `.exception()` 调用点全部只留下一个事件名：

| 位置 | 现在实际输出 |
|------|--------------|
| `app/downloads/service.py:739` | `{"level":"ERROR","event":"download_worker_error","error_code":"DOWNLOAD_WORKER_ERROR"}` |
| `app/conversion/service.py:418` | `{"level":"ERROR","event":"conversion_worker_error",...}` |
| `app/torrent/service.py:279` | `{"level":"ERROR","event":"torrent_poller_error",...}` |
| `app/wiring.py:539` | `{"level":"ERROR","event":"application_startup_failed",...}` |

实测（`ZeroDivisionError` + `exc_info`）输出为 `{"...","event": "pack failed"}` —— 堆栈一个字都没有。

这三处恰好是**防御性 worker 循环**：`except Exception` 兜住一切、记一行、继续跑。
兜底加上不记堆栈，等于「下载工作器出错了，不告诉你哪一行」。这是目前唯一会让线上问题
**无法排查**的缺陷，也是本方案要修的第一件事。它与选哪个库无关，stdlib 就能修。

### 1.2 两种格式在同一个容器里交替输出

`app/server.py` 用 `uvicorn.run(...)`，而 uvicorn 自带 `LOGGING_CONFIG`：
`uvicorn` 与 `uvicorn.access` 两个 logger 都是 `propagate: false` 且**自带 handler**
（`DefaultFormatter` / `AccessFormatter`，`%(levelprefix)s` 文本格式）。
`configure_logging()` 只清了 root 的 handler，动不到它们。

结果有两个：

- 容器日志里 JSON 行与彩色文本行交替出现，任何 `jq` 或采集器都得先分流。
- **访问日志不过脱敏**。`redact_sensitive_values` 只活在 `JsonFormatter` 里，
  `AccessFormatter` 是另一条路。请求行里的 query string 因此原样落盘。
  当前没有凭据走 query（都在 POST body 与 `data/private/`），所以这不是已发生的泄露，
  但「凭据永不进日志」这条铁律现在**只被一半的输出路径保证**，属于承诺与实现不符。

### 1.3 没有留存与轮转（会填满磁盘）

只写 stdout；`compose.yaml` 与 `compose.deploy.yaml` **都没有 `logging:` 段**，
于是走 Docker 默认的 `json-file` 驱动且**不限大小**。一个 `restart: unless-stopped`
的长跑服务，加上每 2 秒一次的前台轮询产生的访问日志，日志文件只增不减直到磁盘满。
这是与选库无关的部署缺陷，一个 compose 片段就能修。

### 1.4 其余较小的缺口

- **不可调级别**：`configure_logging()` 里硬编码 `logging.INFO`，没有 `LOG_LEVEL`
  环境变量。想开某一个子系统的 debug 只能改代码重新打包。
- **载荷里没有来源**：payload 只有 `timestamp` / `level` / `event` 加五个可选字段
  （`candidate_id`、`job_id`、`source_type`、`duration_ms`、`error_code`）。
  **没有 logger 名、模块、行号**，所以看到一条 warning 无法知道是哪个子系统发的。
- **无请求/任务关联 id**：一次「通过候选」跨 review → enqueue → worker 三处，
  日志之间没有任何可关联的键；`candidate_id` 靠每个调用点手写 `extra` 传，漏了就没有。
- **脱敏偏激进且脆弱**：`_URL_QUERY` 把任何 `?` 之后整段替换掉
  （现有测试甚至断言 `safe=value` 的 `value` 也不得出现），排查时会丢掉有用信息；
  而正则跑在**已格式化的字符串**上，加一个新的敏感字段就要再加一条正则。
- **运维者在页面上看不到日志**：工作台只呈现 `app.state.startup_errors`。
  一个「单管理员 + Web 界面」的服务，出问题却必须去宿主机敲 `docker compose logs`。
- **`configure_logging()` 在 `create_app()` 里**：每建一个 app 就清一次 root handler。
  测试里建 app 是常事，这让日志断言与 `caplog` 的行为取决于调用顺序。
## 2. 需求：这个项目的日志要回答什么问题

选库之前先定验收标准。EhBot 是**单容器、单管理员、SQLite、两个后台 worker** 的自托管服务，
不是一个多副本的云上系统。这决定了很多「业界最佳实践」在这里是过度设计。

必须回答的四个问题：

1. **「这本书为什么没下来/没打包？」** —— 需要按 `candidate_id` / `job_id` 串起一条链，
   且失败时**带堆栈**。这是主诉求。
2. **「昨天半夜服务是不是重启了、为什么？」** —— 需要留存，即日志要活得比容器长。
3. **「有人登录过吗、改过什么设置？」** —— 安全相关的动作要能追溯。
   注意 `review_actions` 表已经承担了审核动作的审计，日志不该复制它。
4. **「现在慢在哪？」** —— 需要请求耗时与外部调用耗时。`duration_ms` 字段已预留但几乎没人写。

三条硬约束：

- **凭据永不进日志**（`AgentHelp/AGENTS.md` 的铁律）。任何方案都必须覆盖**全部**输出路径，
  包括 uvicorn 的访问日志，不能只覆盖自己的 formatter。
- **不新增外部服务依赖**。项目卖点是「单容器、无外部数据库」，方案不能要求操作者先跑一个
  Loki 或 Elasticsearch。可选接入是好的，必需就不行。
- **依赖要克制**。当前生产依赖只有 9 个，`uv.lock` 完全锁定。为日志加一棵大依赖树不值得。

## 3. GitHub 上的候选方案与取舍

以下数据实测于 2026-08-29（GitHub API + PyPI JSON API）：

| 候选 | star | 最后提交 | 最新版 | 发布时间 | 传递依赖 |
|------|------|----------|--------|----------|----------|
| `structlog` (hynek) | 4,934 | 2026-08-06 | 26.1.0 | 2026-06-06 | 无（Py≥3.11） |
| `loguru` (Delgan) | 24,089 | 2026-08-23 | 0.7.3 | 2024-12-06 | Windows 上 2 个 |
| `python-json-logger` (nhairs) | 267 | 2026-08-15 | 4.2.0 | 2026-08-15 | 无 |
| `python-json-logger` (madzak，原作) | 1,759 | 2024-12-12 | — | — | **已归档** |
| `sentry-sdk` | — | — | 2.68.1 | 2026-08-24 | `urllib3`, `certifi` |

两个值得注意的事实：`loguru` 星数最高（是 `structlog` 的四倍多），
但最新发布已是 2024-12；`python-json-logger` 原仓库**已被作者归档**，
现在维护的是 `nhairs` 的 fork——若当初选了它，现在正在做迁移。

### 3.1 `structlog`（结构化日志的事实标准）

- **给什么**：`logger.bind(candidate_id=7)` 形式的上下文绑定 + `contextvars` 自动透传，
  处理器（processor）管道，可与 stdlib `logging` 双向桥接。
- **对上文的价值**：直接解决 1.4 的「无关联 id」与「`extra` 靠手写」——绑一次，
  该协程内后续所有日志自动带上 `candidate_id`，不必每个调用点重复写 `extra`。
  脱敏也能从「正则跑格式化后的字符串」升级为「processor 按 key 处理结构化字段」，
  比现在稳。
- **代价**：**实测零传递依赖**——PyPI 元数据里唯一的 `typing-extensions` 只在 Python < 3.11 生效，
  而本项目 `requires-python = ">=3.12"`。但全仓 60 余处调用点若要吃到
  `bind()` 的好处需要逐步迁移；桥接模式下可以不动存量代码。
- **判断**：**推荐，但作为第二阶段**。它解决的是「日志质量」，不是当下最痛的「堆栈没了」。

### 3.2 `loguru`（开箱即用，含轮转）

- **给什么**：一个 `logger.add("file.log", rotation="10 MB", retention="14 days",
  compression="zip", serialize=True)` 就同时给到文件留存、轮转、压缩、JSON 序列化，
  并且**默认带完整 traceback（含变量值）**。
- **对上文的价值**：1.1 与 1.3 一行搞定。
- **代价**：它要**接管**全局 logger，与 stdlib `logging` 的融合是靠 `InterceptHandler`
  转发，属于「换一套体系」而不是「补一个 formatter」。本仓已有 60 余处 stdlib 调用点、
  一个自己的 `JsonFormatter` 和针对它的测试；换体系会把这些全部作废。
  另外已**实测确认**：`loguru/_defaults.py` 里 `LOGURU_DIAGNOSE` 默认为 `True`，
  即带变量值的 traceback 是**开箱就开的**——它会把局部变量打进日志。
  对一个局部变量里随时可能持有 token、Cookie 与会话串的服务，
  这是与铁律直接冲突的默认值，必须显式关掉。且其最新发布 0.7.3 停在
  2024-12，虽然仓库仍有提交（star 24k，是本表里最高的）。
- **判断**：**不采用**。它最适合从零起步的脚本，而这里已经有一套能用的 stdlib 管道。

### 3.3 `python-json-logger`

- **给什么**：把 `LogRecord` 转 JSON，可选字段可配。
- **判断**：**不采用**，因为 `app/logging.py` 已经在做同一件事，且带了本项目特有的脱敏。
  换成它等于把自己的脱敏逻辑重写成它的 formatter 子类，净收益接近零。
  联网核对又多一条理由：原仓库 `madzak/python-json-logger` 已被作者**归档**，
  维护权转到 `nhairs` 的 fork。为一个净收益接近零的替换去跟一次仓库迁移，不划算。

### 3.4 `logging.handlers.RotatingFileHandler`（stdlib）

- **给什么**：文件留存 + 按大小轮转 + 保留 N 份。零依赖。
- **代价**：无压缩；多进程写同一文件不安全（本项目 `workers=1`，不受影响）。
- **判断**：**采用**。这是 1.3 的正解——留存是运维需求，不值得为它引入一棵依赖树。

### 3.5 集中式采集（Loki + Promtail / Vector / Grafana Alloy）

- **给什么**：容器 stdout 直接被抓走，查询、告警、长期留存都在外部。
- **判断**：**不纳入本方案，但要为它保持兼容**。真正需要集中式日志的操作者已经有自己的栈；
  我们的义务是**输出干净的单行 JSON 到 stdout**（即修掉 1.2 的双格式问题），
  他们就能零成本接。反过来，要求每个自托管用户先部署 Loki 是违反「单容器」卖点的。

### 3.6 `sentry-sdk` / OpenTelemetry

- **判断**：**不采用**。前者要往外发数据，与「自托管、不外联」的定位冲突（且默认会捕获
  局部变量与请求体）；后者是分布式追踪，对单进程两 worker 的服务是纯负担。
  如果以后要，`structlog` 的结构化输出是它们最好的入口。

### 3.7 结论

**不换体系，补齐 stdlib 管道；库只加 `structlog`，且放在第二阶段。**

理由是本仓的实际情况：已有的 `JsonFormatter` + 脱敏是资产而不是负债，
最痛的三个问题（丢堆栈、双格式、无留存）**全部可以用 stdlib 修**，
一个都不需要新依赖。先把这些修掉，日志立刻可用；`structlog` 带来的
「上下文自动透传」是锦上添花，等真的需要跨 worker 关联时再上，且届时可以
桥接接入而不必改动存量调用点。
## 4. 方案

分三阶段，**每阶段独立可交付、可停在任意阶段**。阶段 L1 零新依赖。

### L1：修好现有管道（零新依赖，建议无论如何都做）

**L1.1 堆栈进 JSON。** `JsonFormatter.format()` 读 `record.exc_info`，
用 `self.formatException()` 得到文本后放进 `exc_info` 字段（**同样过脱敏**，
因为异常消息里可能带 URL）。`record.stack_info` 同理。
这一条修完，四个 `.exception()` 调用点才真的在报告异常。

**L1.2 载荷补来源与关联字段。** 增加 `logger`（`record.name`）、`module:lineno`；
可选字段表从 5 个扩到包含 `request_id`、`work_id`、`provider`、`status`、`attempt`。
`logger` 名是最便宜的定位信息——`app.torrent.service` 一眼就知道是哪个子系统。

**L1.3 收编 uvicorn 的两个 logger。** `configure_logging()` 里显式：

```python
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = True
```

于是访问日志也走 `JsonFormatter`，**顺带自动获得脱敏**，容器里只剩一种格式。
`app/server.py` 的 `uvicorn.run()` 需要传 `log_config=None`，
否则它会在启动时用自带 `LOGGING_CONFIG` 再覆盖一次。
**这条是安全修复**，不只是格式统一。

**L1.4 级别可配。** 新增 `LOG_LEVEL`（默认 `INFO`）与 `LOG_ACCESS`（默认 `true`，
关掉可消除前台轮询产生的访问日志噪音）。

> **实测发现的坑**：不能用 uvicorn 自己的 `access_log=False` 来关。
> 读 `uvicorn.config.Config.configure_logging` 的源码：`access_log is False` 时它做的是
> `handlers = []` **加 `propagate = False`**。后一半会把 L1.3 刚接好的管道又断开，
> 而且是静默断开。所以 `LOG_ACCESS=false` 必须实现为「给 `uvicorn.access` 装一个
> 滤掉全部记录的 `Filter`」或直接把它的 level 抬到 `WARNING`，而不是转给 uvicorn 处理。放在 `app/config.py` 的 `Settings`，
与其余环境变量同一处读取。**注意**：按本仓既有约定，
「一次性、部署级」的参数才用环境变量；日志级别属于此类，不进 `system_settings` 表。

**L1.5 文件留存 + 轮转。** `RotatingFileHandler` 写 `data/logs/ehbot.log`，
`maxBytes` 与 `backupCount` 由 `LOG_FILE_MAX_BYTES` / `LOG_FILE_BACKUPS` 控制
（默认 10 MB × 5 份 = 上限 50 MB）。stdout 保留不变（容器与集中式采集都要它）。
落在 `data/` 下是因为那个目录已经是绑定卷且已在文档里被强调「务必保留」。
**需要在 `.dockerignore` / `.gitignore` 层面确认 `data/` 已被忽略** —— 已确认被忽略。

**L1.6 compose 加日志上限。** 两个 compose 文件都加：

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

这与 L1.5 不重复：前者管 Docker 抓走的 stdout，后者管容器内的文件；
只做一个都会在另一条路上把磁盘填满。

**L1.7 `configure_logging()` 移出 `create_app()`。** 改为幂等（已配置则直接返回），
并在 `app/server.py` 的 `main()` 里显式调用一次。测试里反复建 app 不再重置 root handler。

### L2：让日志能回答业务问题（仍零新依赖）

**L2.1 请求 id 中间件。** 每个请求生成一个短 id（或采信可信代理的 `X-Request-ID`），
存进 `contextvars`，由一个 `logging.Filter` 注入到该请求期间的每条记录。
这样一次操作的所有日志可以串起来，也能把 JSON 错误响应与日志行对上。

**L2.2 任务生命周期日志成为约定。** 两个 worker 在领取 / 完成 / 失败时各记一条，
统一带 `job_id`、`candidate_id`、`provider`、`attempt`、`duration_ms`。
现在这些字段全靠调用点自觉，导致 `duration_ms` 几乎没人填。
**这是本项目日志的主要价值所在**——第 2 节的问题 1 就靠它回答。

**L2.3 一个只读的日志页面。** 「设置 → 系统」下加一个尾读视图，
读 `data/logs/ehbot.log` 的最后 N 行并按级别筛选。理由：这是个**单管理员的 Web 服务**，
让操作者为看一行报错去宿主机敲 `docker compose logs` 是这个产品形态的缺口。
**约束**：只读、只尾读（不允许任意路径）、复用现有会话鉴权、
且**不新增第二套状态词表**（级别名按 `app/api/status.py` 的既有做法处理）。

### L3：可选增强

- **`structlog` 桥接**，获得 `bind()` 与自动上下文透传，脱敏改为按结构化字段处理。
  存量 `logging.getLogger(__name__)` 调用点不必改动。
- **`/metrics` 或 JSON 形式的健康摘要**，供外部监控轮询（当前只有 `/healthz`、`/readyz`）。
- **文档一节**：`docs/USAGE.md` 说明如何把 stdout 接到 Loki / Vector。

## 5. 影响面与风险

| 改动 | 风险 | 处理 |
|------|------|------|
| 收编 `uvicorn.access` | 访问日志格式变化，可能打断操作者已有的 grep 习惯 | 在 `docs/USAGE.md` 与 progress 记录；提供 `LOG_ACCESS=false` |
| `uvicorn.run(log_config=None)` | 传错会导致启动时**完全无日志** | 必须有一条集成测试断言启动横幅仍然出现 |
| 堆栈进日志 | 异常消息可能含敏感串 | `exc_info` 文本**必须**过 `redact_sensitive_values` |
| 写文件 | `data/` 不可写时不能让服务起不来 | 文件 handler 失败降级为「仅 stdout」并记一条 warning，不抛 |
| 日志页面 | 新的读取面 = 新的攻击面 | 只尾读固定路径、会话鉴权、限制返回行数 |
| 轮转 | `workers=1` 才安全 | 已确认；若将来多 worker 需换 `WatchedFileHandler` + 外部轮转 |

**测试基线**：当前 985 passed / 0 failed。L1 预计新增 15–20 条（脱敏后的堆栈、
访问日志走 JSON、级别可配、文件轮转降级、幂等配置），L2 再加 10–15 条。
`tests/unit/test_logging.py` 现有两条脱敏断言必须继续通过——它们是铁律的守卫。

## 6. 建议的取舍

**只做 L1，就已经把「没有完善日志管理」这句话消掉。** 它零新依赖，
修掉一个会让线上问题无法排查的缺陷（丢堆栈）、一个安全承诺缺口（访问日志不脱敏）、
一个会填满磁盘的部署缺陷（无轮转），改动集中在 `app/logging.py`、`app/config.py`、
`app/server.py` 与两个 compose 文件。

**L2 是让日志真正好用的部分**，尤其 L2.2 的任务生命周期字段与 L2.3 的页面。

**L3 及 `structlog` 可以一直不做**，除非以后真的需要跨请求上下文透传。

## 7. 联网核对结果（2026-08-29）

初稿把这一节写成了「待核对清单」，因为我当时误以为沙箱无法联网。
实际上沙箱只是**默认**拦网络，提权后可以访问。四项均已核完：

1. **`structlog` 仍活跃**：4,934 star，最后提交 2026-08-06，最新 26.1.0（2026-06-06）。
   传递依赖对 Python 3.12 而言**为零**。结论不变：可以用，但放 L3。
2. **无更合适的新库**。按 star 搜了一轮结构化日志库，前列要么是应用仓库误匹配，
   要么是不过百星的小项目（最大的 `python-logfmter` 也只有 107 star）。
   预期得到验证：这个领域已经固定在 `structlog` / `loguru` 两强。
3. **`loguru` 的 `diagnose` 确实默认开启**（源码实读，见 3.2）。否决理由成立。
4. **`max-size` / `max-file` 写法正确**。本机 Docker 29.2.1，
   `docker info` 报告默认驱动就是 `json-file`——即 1.3 说的「无上限地消耗磁盘」是真实当前行为。

另有一项不在原清单、但核对时顺手发现的事实，已写回 L1.4：
uvicorn 的 `access_log=False` 会连带设上 `propagate = False`，
因此不能用它来实现 `LOG_ACCESS=false`。这是实施时很容易踩的陷。