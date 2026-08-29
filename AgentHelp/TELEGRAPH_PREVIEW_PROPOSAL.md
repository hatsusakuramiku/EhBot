# Telegraph 预览页图源方案（已确认，TELEGRAPH 分支细化设计）

> **文档定位**：本文件先于 `DOWNLOAD_SOURCE_CHAIN_PROPOSAL.md` 写成，当时把预览页当作唯一新增图源。
> 阶段 14 的**计划之源是 `DOWNLOAD_SOURCE_CHAIN_PROPOSAL.md`**，它给出四级链
> `TELEGRAM → EH_TORRENT → TELEGRAPH`；本文件保留为其中 **TELEGRAPH 一环的细化设计**
> （模块划分、安全门禁、错误码、配置项、测试清单仍然有效，实现时以本文件为细则）。
> 两处已被取代，正文中就地标注：第 7 节的路由表（未含 EH_TORRENT）、第 9 节的迁移文件名。

## 1. 目标与边界

Telegram Bot API 的 `getFile` 对单文件有 20 MB 硬上限，超限的压缩包附件永久失败（`TELEGRAM_FILE_TOO_BIG`，见 `progress.md` 阶段 13）。频道为方便阅读通常另附一个 `telegra.ph` 预览页，页内是整本图片，且消息里同时给出 `原始地址` 的 ExHentai 画廊链接。

本方案新增一个下载来源 `TELEGRAPH`：解析预览页取出有序图片，直接经 HTTPS 抓取并打包，交给已有归档流水线产出 CBZ。元数据仍走已实现的 gdata 链路，不受影响。

本文件只定义方案，不含实现。

## 2. 实测依据

数据来自 `.Ref/ChatExport_2026-08-21/messages.html` 的真实频道消息，并在 2026-08-21 实际抓取核对。

| 项目 | 实测结果 |
|------|----------|
| 预览链接形态 | `https://telegra.ph/<slug>`，URL 位于 caption 的 `text_link` entity 中，**不出现在纯文本里** |
| 解析方式 | `GET https://api.telegra.ph/getPage/<path>?return_content=true` 返回 `ok:true` 与有序节点树，深度遍历取 `img.attrs.src` |
| 图片宿主 | 不是 `telegra.ph/file/`，而是各频道自建的 Telegram 文件代理：`image.dangernsfw.win`（`<sha256>.webp`）、`pic.850123.xyz`（路径即 base64 `file_id`），均在 Cloudflare 后 |
| 防盗链 | 裸请求返回 403；带 `User-Agent` 与 `Referer: https://telegra.ph/` 返回 200 |
| 页数完整性 | 完整。三本实测 22/22、15/15、78/78，与 ExHentai gdata `filecount` 逐本吻合 |
| 画质 | **非原图**。gid 1655718 原档 145,185,851 B / 15 页（9.7 MB/页），预览页 7,895,214 B / 15 页（0.53 MB/页）；两个图床都统一缩到宽 1280，格式为 JPEG 或 WebP |

结论：预览页图源页数完整、能绕开 20 MB 上限、无需 ExHentai Cookie，但成品是 1280px 重编码版，约为原档体积的 5%–10%。**定位为阅读级兜底，不替代原档。**

同类实现参考 gallery-dl 的 `gallery_dl/extractor/telegraph.py`（HTML `<figure>` / `<img>` 抓取、文档序编号）。本方案优先用官方 API，HTML 解析仅作降级。

## 3. 消息形态

导出样本中存在三种频道形态：

| 形态 | 消息内容 | 可用来源 |
|------|----------|----------|
| 预览 + 原始地址 | 标签行、`预览:` 超链接指向 `telegra.ph`、`原始地址:` 指向画廊 | TELEGRAPH、EXHENTAI |
| 附件 + 原始地址 | 压缩包附件（样本中 138.7 MB）、`频道：t.me/c/...`、`原始地址:` | TELEGRAM（≤20 MB 时）、EXHENTAI |
| 纯预览 | 只有预览页链接 | TELEGRAPH |

第三种形态目前会被 `CandidateIngestor._parse_message` 判为「未包含图片预览、ExHentai 链接或压缩包附件」而忽略，本方案将其纳入候选。

`t.me/c/...` 形式的频道消息链接不在本方案范围内。

## 4. 处理流水线

`ConversionService._enqueue_sync` 取的是「最近一个 `COMPLETED` 下载任务的 `ARCHIVE` artifact」。因此新来源只需产出一个 ZIP 并登记为 `ARCHIVE` artifact，其后的安全校验、CBZ 打包、ComicInfo 注入和原子发布**完全复用，不作修改**（`zipfile-default` profile 本就是流式 `ZIP_STORED` 转写，多这一层几乎无成本）。

```text
telegra.ph 页面 URL
  -> getPage API 取有序 img 列表（失败降级 HTML）
  -> 逐张抓取（并发上限 / 安全门禁 / 魔数校验）
  -> 与 gdata filecount 比对页数
  -> 打包为 ZIP_STORED 的 images.zip
  -> 登记 ARCHIVE artifact 并置 COMPLETED
  -> 既有归档流水线 -> CBZ 发布
```

## 5. 模块与接口

新增 `app/telegraph/`，写法对齐既有 `app/exhentai/`（`work_path_provider` 解析目录、DB 调用包 `asyncio.to_thread`、错误对象带 `code` 与 `public_message`）。

| 文件 | 职责 |
|------|------|
| `models.py` | `TelegraphPage(path, url, title, author, image_urls)`、`TelegraphError(code, message)` |
| `client.py` | `fetch_page(url)`：API 优先，`ok:false` 或结构异常时降级 HTML；`/embed/` 跳过，`/` 开头补 `https://telegra.ph`；文档序保序去重 |
| `guard.py` | URL 门禁：scheme、宿主、DNS 解析后的地址判定、重定向逐跳复检 |
| `fetcher.py` | 并发抓取、逐张重试、防盗链头、零填充命名 `0001.jpg`、魔数校验 |
| `packer.py` | `ZIP_STORED` 打包到 `work/telegraph/candidate-<id>.zip` |
| `service.py` | `TelegraphService.download_for_candidate(candidate_id)`，登记 artifact 与来源留痕 |

## 6. 安全门禁

图片 URL 来自不可信的第三方内容，必须显式约束：

- 页面宿主白名单：仅 `telegra.ph`、`graph.org`。
- 图片仅允许 `https`；DNS 解析后拒绝回环、私网、链路本地与 IPv6 ULA（SSRF）。
- 重定向上限 3 跳，每跳重新过门禁；非 http(s) scheme 直接拒绝。
- 单图上限 20 MiB，单本上限 400 张 / 1 GiB，单本总超时 600 秒。
- 每张必须通过 `app/archive/safety.py` 的图片魔数校验（已支持 jpg/png/webp/gif/bmp/avif/jxl）；SVG、HTML、零字节一律拒绝。
- 使用独立 `httpx.AsyncClient`，不携带任何 Cookie 或 Token；Telegram 与 ExHentai 凭据不出现在这条链路上。
- 抓取写入任务专属临时目录，失败清理，成品原子发布。

## 7. 路由与降级

确认采用**自动降级**，原档优先：

> **本节已被取代**：以下路由表写于 EH_TORRENT 方案之前，且当时把 EXHENTAI（归档下载）当作自动来源。
> 现行路由见 `DOWNLOAD_SOURCE_CHAIN_PROPOSAL.md` 第 5 节：`TELEGRAM → EH_TORRENT → TELEGRAPH`，
> EXHENTAI 归档下载因消耗 GP 降级为手动按钮。TELEGRAPH 仍是链尾兜底，这一点不变。

```text
有压缩附件且 ≤20 MB      -> TELEGRAM
否则有 ex_gid 且 Cookie 已配置 -> EXHENTAI
否则有 preview_url        -> TELEGRAPH
都没有                    -> CANDIDATE_NOT_DOWNLOADABLE
```

补充两条：

- Telegram 任务以 `TELEGRAM_FILE_TOO_BIG` 失败且候选存在 `preview_url` 时，自动入队 TELEGRAPH 任务，并在原任务 `error_message` 追加「已自动改用预览图源」。
- 审核详情页与「下载任务」页提供手动「用预览页下载」按钮，沿用现有 CSRF 与 `_download_action` 写法。

## 8. 状态、错误码与留痕

新增 provider `TELEGRAPH`，沿用既有下载任务状态机，不新增状态。

| 错误码 | 含义 | 可重试 |
|--------|------|--------|
| `TELEGRAPH_PAGE_UNREACHABLE` | 预览页请求失败 | 是 |
| `TELEGRAPH_PAGE_PARSE` | 页面结构无法解析 | 是 |
| `TELEGRAPH_NO_IMAGES` | 页面没有图片 | 否 |
| `TELEGRAPH_IMAGE_BLOCKED` | 图片 URL 未通过安全门禁 | 否 |
| `TELEGRAPH_IMAGE_FAILED` | 图片抓取或校验失败 | 是 |
| `TELEGRAPH_LIMIT_EXCEEDED` | 超出张数、单图或总量上限 | 否 |
| `TELEGRAPH_PAGE_COUNT_MISMATCH` | 图片数与 gdata `filecount` 不一致 | 是 |

页数不一致时（预览页被拆成 `Page-1` / `Page-2`，或图床缺图）**不发布残本**：任务失败并置该错误码，候选走既有 `NEEDS_INFO` 通道，提示「预览页只有 N/M 页」；人工补第二个预览链接后重新入队复用同一任务。

来源留痕采用 ComicInfo 加数据库，不改文件名（避免日后回填原档破坏书库索引）：

- `download_jobs.details_json` 记录页面 URL、图床、张数、总字节、平均宽度。
- 写入一条 `metadata_values`：`field_name='ScanInformation'`、`value_source='TELEGRAPH'`、值形如 `TELEGRAPH_PREVIEW w1280 15p 7.5MiB`；`build_comicinfo_xml` 增加 `scan_information` 参数输出 `<ScanInformation>`，由现成的 `_metadata_lookup` 带出，不改调用链签名。

## 9. 数据模型变更

新增迁移 `app/db/migrations/010_telegraph_preview.sql`：

```sql
ALTER TABLE candidates ADD COLUMN preview_url TEXT;
ALTER TABLE source_messages ADD COLUMN preview_urls_json TEXT;
```

> **实际落地为 `app/db/migrations/010_download_sources.sql`**（同时带上 torrent 两列），
> 且 `preview_urls_json` 取 `TEXT NOT NULL DEFAULT '[]'` 以免读取侧处理 NULL。以已落库的迁移为准。

`save_candidate_message` 按 `ex_gid` 的既有写法回填 `preview_url`：为空则写入，已有则不覆盖。`ParsedSourceMessage` 增加 `preview_urls: tuple[str, ...]`。

`CandidateIngestor._parse_message` 目前只对 `text` 做正则，必须增加 `entities` / `caption_entities` 中 `text_link` 的 `url` 提取——ExHentai 链接是明文所以现在能抓到，预览链接是超链接所以现在抓不到。

## 10. 配置项

| 变量 | 默认值 |
|------|--------|
| `TELEGRAPH_ENABLED` | `true` |
| `TELEGRAPH_CONCURRENCY` | `3` |
| `TELEGRAPH_MAX_IMAGES` | `400` |
| `TELEGRAPH_MAX_IMAGE_BYTES` | `20971520` |
| `TELEGRAPH_MAX_TOTAL_BYTES` | `1073741824` |
| `TELEGRAPH_TIMEOUT_SECONDS` | `600` |
| `TELEGRAPH_REQUIRE_FILECOUNT_MATCH` | `true` |

## 11. 测试方案

单元（`tests/unit/test_telegraph.py`）：节点树深度遍历与保序去重、相对 `/file/` 补全、`/embed/` 跳过、HTML 降级、entity 与明文混排提取、宿主白名单、SSRF 判定、单图与总量与张数超限、魔数拒绝 SVG 与 HTML、零填充命名。

集成（`tests/integration/test_telegraph_workflow.py`，`httpx.MockTransport` 模拟 API 与图床）：审核通过至 CBZ 落地且含 `ScanInformation`；`TELEGRAM_FILE_TOO_BIG` 自动降级只产生一个 TELEGRAPH 任务；页数不符时候选进 `NEEDS_INFO` 且不发布；防盗链 403 首次失败、带 Referer 重试成功；新 provider 能被 worker 领取。

`DownloadService._claim_pending_job_sync` 现在把 provider 写死为 `IN (?, ?)`，改为按 `SUPPORTED_PROVIDERS` 展开占位符，并以测试锁定——不改会静默漏领新 provider 的任务。

真实网络用例按 `tests/integration/test_seven_zip_real.py` 的既有约定默认 skip。

## 12. 交付顺序

1. 本方案落档，并在 `task_plan.md` 登记阶段、`findings.md` 记录实测结论。
2. 迁移与摄取：entity 提取、`preview_url` 落库、纯预览消息纳入候选。
3. `app/telegraph/`：客户端、门禁、抓取、打包。
4. 下载队列接线：provider 列表修复、新分支、错误码。
5. 自动降级路由、审核页按钮、`NEEDS_INFO` 分支。
6. ComicInfo `ScanInformation`、配置项、README 小节。
7. 全量 `pytest`，并用样本中的四个真实页面（含 78 页那本）做一次带网络的手工验证，把张数、总字节与耗时记入 `progress.md`。

## 13. 暂不承诺

- 不引入 MTProto / Telethon。它才是取得原档的真解，但需要 `api_id`、`api_hash` 与用户会话，属独立产品决策。
- 不解码第三方图床路径中的 `file_id` 去调用本项目 Bot 的 `getFile`：跨 Bot 的 `file_id` 对 `getFile` 不可用。
- 不对抓取到的图片做任何重编码、放大或格式转换。
- 不处理 `t.me/c/...` 频道消息链接。
- 不缓存或镜像第三方图床内容；图床失效即任务失败，由操作者决定改走 ExHentai。
