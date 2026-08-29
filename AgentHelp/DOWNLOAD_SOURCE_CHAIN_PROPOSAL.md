# 下载来源与降级链路方案（待确认）

## 1. 目标与范围变更

Telegram Bot API 的 `getFile` 对单文件有 20 MB 硬上限，超限附件永久失败（`TELEGRAM_FILE_TOO_BIG`，见 `progress.md` 阶段 13）。样本频道里超限的本子是常态（导出中一本 138.7 MB）。

本方案把「下载来源」从两条（Telegram 附件、ExHentai 归档）扩展为一条**四级降级链路**，以 EH 画廊为唯一元数据与原档锚点：Telegram 附件 → EH 种子 → EH Archive Download（手动）→ Telegraph 预览页兜底。

**本方案显式推翻 `DEVELOPMENT_PLAN.md` 3.2 中的两条边界**，需要在批准时一并确认：

| 原边界 | 变更后 |
|--------|--------|
| 不默认使用 Torrent 下载 | EH 种子成为超限本子的**首选**原档来源。理由：种子是免费的原档，Archive Download 消耗 GP |
| 不默认在 Ex 归档失败后无限制逐页抓图 | 仍然不逐页抓 EH 画廊。抓取对象是频道自建的 `telegra.ph` 预览页，且有张数、字节和超时上限，不是无限制 |

本文件只定义方案，不含实现。

## 2. 降级链路

```text
有压缩附件且 ≤20 MB        -> TELEGRAM     原档、免费、最快
否则 gdata torrentcount>0  -> EH_TORRENT   原档、免费、依赖做种者
否则有 preview_url         -> TELEGRAPH    1280px 重编码、兜底
都没有                     -> CANDIDATE_NOT_DOWNLOADABLE
```

三条已确认的决策：

1. **Archive Download 降为纯手动**。它消耗 GP，不进自动路由；审核详情页保留「用 Archive Download 取原档」按钮。这会改变现有行为：目前 `_approve_candidates_and_enqueue` 在无附件时自动入队 `EXHENTAI`。
2. **卡种不自动降级**。种子推送后无做种者时，任务停在「等待种子」状态并在页面显示停滞时长与 peer 数，由操作者点「改用预览图源」或「用 Archive Download」。
3. **种子客户端外接 qBittorrent WebAPI**。本项目不在主进程内跑 P2P，不引入 libtorrent，符合既有「主进程不加载不受控 DLL」与 1C/512 MB 低配目标。

## 3. 实测依据

数据取自 `.Ref/ChatExport_2026-08-21/messages.html` 的真实频道消息，并于 2026-08-21 实际请求核对。

### 3.1 gdata 已直接返回种子信息

`method=gdata` 的响应里本来就有 `torrentcount` 与 `torrents`，**发现种子不需要额外请求，也不需要 Cookie**：

```json
{"hash": "4acbd66e5d0518977ece30c343eb75c4ca92b031",
 "added": "1786287412",
 "name": "[三崎 (おくらほこり)] ....zip",
 "tsize": "10119", "fsize": "126838245"}
```

| gid | filecount | filesize | torrentcount | 种子 fsize |
|-----|-----------|----------|--------------|-----------|
| 4108964 | 78 | 139,262,241 | 1 | 126,838,245 |
| 4076223 | 45 | 153,242,638 | 1 | 145,532,213 |
| 1655718 | 15 | 145,185,851 | **0** | — |

两点结论：

- 种子内容是上传者的原始 `.zip`，`fsize` 与画廊 `filesize` **不相等**（126.8 MB vs 139.2 MB）。因此**页数一致性门禁只对预览页生效，不能套用到种子上**。
- `torrentcount=0` 真实存在。链路缺任何一级都会断，四级必须都实现。

### 3.2 预览页实测

| 项目 | 实测结果 |
|------|----------|
| 链接形态 | `https://telegra.ph/<slug>`，URL 在 caption 的 `text_link` entity 中，**不在纯文本里** |
| 解析方式 | `GET https://api.telegra.ph/getPage/<path>?return_content=true` 返回 `ok:true` 与有序节点树，深度遍历取 `img.attrs.src` |
| 图片宿主 | 不是 `telegra.ph/file/`，而是频道自建的 Telegram 文件代理：`image.dangernsfw.win`（`<sha256>.webp`）、`pic.850123.xyz`（路径即 base64 `file_id`），均在 Cloudflare 后 |
| 防盗链 | 裸请求 403；带 `User-Agent` 与 `Referer: https://telegra.ph/` 返回 200 |
| 页数完整性 | 完整。三本实测 22/22、15/15、78/78，与 gdata `filecount` 吻合 |
| 画质 | **非原图**。gid 1655718 原档 145,185,851 B / 15 页（9.7 MB/页），预览页 7,895,214 B / 15 页（0.53 MB/页），统一缩到宽 1280 |

预览页页数完整、无需 Cookie、能绕开 20 MB 上限，但约为原档体积的 5%–10%。**只做最后兜底。**

同类实现参考 gallery-dl 的 `gallery_dl/extractor/telegraph.py`（HTML `<figure>` / `<img>` 抓取、文档序编号）；本方案优先用官方 API，HTML 解析仅作降级。

## 4. 元数据

不变，仍以 EH 为唯一权威源：`原始地址` 解析出 `gid`/`token` → `gdata`（免 Cookie、带命名空间标签）→ EhTagTranslation 中文化 → `metadata_values`。已在阶段 5/8 实现。

本方案只增加一处消费：`gdata` 响应中的 `torrentcount` / `torrents` 需要落库，作为选路依据（见 §9）。

## 5. EH 种子来源

### 5.1 选种策略

`torrents` 有多条时按序打分：排除名称含 `resample` 的重采样版本 → `fsize` 最接近 gdata `filesize` 者优先 → `added` 最新者优先。gdata 不提供做种数，无法按 seeds 选，卡种由 §5.4 处理。

### 5.2 取 .torrent 文件

`torrents[].hash` 只是 infohash，**不是下载链接**。取文件需要登录态：请求 `https://exhentai.org/gallerytorrents.php?gid=<gid>&t=<token>`，解析页面中该 hash 对应的 `.torrent` 链接后下载。链接模板不硬编码，一律从页面解析。

三条安全约束：

- .torrent 的 announce 内含账号 passkey，**属于凭据**。文件只落在工作目录，不进书库、不进日志、不进审计。
- 本地用 bencode 计算 infohash 与 gdata 的 `hash` 比对，不一致直接 `TORRENT_FILE_INVALID`。这同时防住页面解析取错条目或拿到过期条目。
- 不构造裸 magnet 替代：EH 种子的 tracker 需要 passkey，且通常置 private 位，磁力链无法宣告。

### 5.3 通过 WebAPI 加种

**加种全程走 qBittorrent WebAPI，不落地共享目录、不用监视文件夹（watch folder）、不碰客户端配置。** EhBot 只需要能访问 qBittorrent 的 HTTP 端口。

| 用途 | 接口 |
|------|------|
| 健康检查 | `GET /api/v2/app/version` |
| 登录 | `POST /api/v2/auth/login`（表单 `username`/`password`，取 `SID` cookie） |
| 读客户端默认保存路径 | `GET /api/v2/app/preferences`（取 `save_path`，供设置页预填） |
| **加种** | `POST /api/v2/torrents/add`（`multipart/form-data`） |
| 查询 | `GET /api/v2/torrents/info?hashes=<hash>` |
| 文件清单 | `GET /api/v2/torrents/files?hash=<hash>`（收货前确认结构） |
| 移除 | `POST /api/v2/torrents/delete`（`hashes`、`deleteFiles`） |

加种请求体：

| 字段 | 值 | 说明 |
|------|----|------|
| `torrents` | .torrent 文件二进制 | multipart 文件字段，`filename` 用 infohash，`Content-Type: application/x-bittorrent` |
| `savepath` | `TORRENT_SAVE_PATH`（客户端视角） | 显式指定，不依赖客户端默认值 |
| `category` | `TORRENT_CATEGORY`（默认 `ehbot`） | 便于操作者在客户端侧一眼分辨来源 |
| `paused` | `false` | |
| `autoTMM` | `false` | 关掉自动种子管理，否则 `savepath` 会被分类规则覆盖 |
| `root_folder` | 不传 | 保留种子自身结构，由 §5.5 按单文件/目录分别收货 |

响应处理：

- `200` 且响应体为 `Ok.` 视为受理；qBittorrent **不返回 hash**，因此随后用 §5.2 本地算出的 infohash 调 `torrents/info?hashes=` 确认落地。
- `415` 表示种子文件无法解析 → `TORRENT_FILE_INVALID`（永久）。
- `403` 表示 SID 失效 → 重登一次后重试，仍失败则 `TORRENT_CLIENT_AUTH`。
- 其他非 2xx → `TORRENT_PUSH_REJECTED`（可重试）。
- 重复加同一 hash 由 qBittorrent 自身吸收，仍返回 `Ok.`，不视为错误。

`torrents/add` 也支持 `urls` 字段传磁力链，本方案不用：EH tracker 需要 passkey 且通常置 private 位，磁力链无法宣告（见 §5.2）。

qBittorrent 的地址与凭据在「归档设置」页登记，凭据按现有密码库做法加密保存（主密钥沿用 `data/private/`），页面不回显。

### 5.4 轮询与卡种

新增下载任务状态 `WAITING_TORRENT`：推种成功后进入，下载工作器不再领取，由一个独立轮询任务（写法对齐 `ConversionService` 的 worker）每 15 秒拉一次 `torrents/info`，把 `progress`、`num_seeds`、`dlspeed`、`eta`、`state` 写入 `details_json`，供「下载任务」页展示。

- `state` 属于 `stalledDL` / `metaDL` 且 `num_seeds=0` 时，页面显示「无做种者，已停滞 N 分钟」，**任务不自动失败、不自动降级**（已确认决策）。
- 操作者动作：「改用预览图源」入队 TELEGRAPH 并从客户端移除该种；「用 Archive Download」入队 EXHENTAI；「取消」同时从客户端移除。
- `state` 为 `error` / `missingFiles` 时任务失败，错误码可重试。

### 5.5 完成收货

`progress == 1` 或 `state` 属于做种态时，读 `content_path`：

- 单文件 `.zip` / `.cbz` / `.rar` / `.7z`（实测 EH 种子正是单个 `.zip`）→ 直接登记为 `ARCHIVE` artifact。
- 目录 → 校验成员为图片后按 `ZIP_STORED` 打包，与预览页路径共用打包器。

两条硬约束：

- **硬链接优先，其次复制，绝不移动**。移动会破坏做种。默认 `TORRENT_KEEP_SEEDING=true`，完成后种子留在客户端继续做种。
- **保存目录对 EhBot 可读**。qBittorrent 把文件下到 `savepath`，EhBot 直接从该路径读取并交给归档流水线。qBittorrent 在另一容器或 NAS 上时两侧看到的路径不同，因此设置项分为 `TORRENT_SAVE_PATH`（客户端视角，随加种请求下发）与 `TORRENT_LOCAL_SAVE_PATH`（EhBot 视角，用于读取）。保存设置时校验后者可读，运行期读不到则 `TORRENT_CONTENT_UNREACHABLE`。

### 5.6 重启与幂等

`idempotency_key = f"torrent:{candidate_id}"`，`details_json` 保存 `hash` 与推送时间。启动时对所有 `WAITING_TORRENT` 任务按 hash 重新挂上轮询；客户端里已不存在该 hash 则置 `TORRENT_VANISHED`（可重试）。重复推同一 hash 由 qBittorrent 自身幂等吸收。

## 6. Telegraph 预览页兜底

> 本节只给链路视角的要点。TELEGRAPH 一环的**细化设计见 `TELEGRAPH_PREVIEW_PROPOSAL.md`**
> （模块划分、SSRF 门禁细则、错误码、配置项、测试清单）。该文件的第 7 节路由表与第 9 节迁移文件名
> 已被本文件取代，那两处在原文中就地标注。

### 6.1 流水线复用

`ConversionService._enqueue_sync` 取的是「最近一个 `COMPLETED` 下载任务的 `ARCHIVE` artifact」。因此预览页来源只需产出 ZIP 并登记 artifact，其后的安全校验、CBZ 打包、ComicInfo 注入与原子发布**完全复用**（`zipfile-default` profile 本就是流式 `ZIP_STORED` 转写）。

```text
telegra.ph 页面 URL
  -> getPage API 取有序 img 列表（失败降级 HTML）
  -> 逐张抓取（并发上限 / 安全门禁 / 魔数校验）
  -> 与 gdata filecount 比对页数
  -> ZIP_STORED 打包
  -> 登记 ARCHIVE artifact 并置 COMPLETED
  -> 既有归档流水线 -> CBZ
```

### 6.2 模块

新增 `app/telegraph/`，写法对齐既有 `app/exhentai/`（`work_path_provider` 解析目录、DB 调用包 `asyncio.to_thread`、错误对象带 `code` 与 `public_message`）。

| 文件 | 职责 |
|------|------|
| `models.py` | `TelegraphPage(path, url, title, author, image_urls)`、`TelegraphError` |
| `client.py` | `fetch_page(url)`：API 优先，`ok:false` 或结构异常降级 HTML；跳过 `/embed/`，`/` 开头补 `https://telegra.ph`；文档序保序去重 |
| `guard.py` | URL 门禁：scheme、宿主、DNS 解析后地址判定、重定向逐跳复检 |
| `fetcher.py` | 并发抓取、逐张重试、防盗链头、零填充命名 `0001.jpg`、魔数校验 |
| `packer.py` | `ZIP_STORED` 打包（种子目录收货共用） |
| `service.py` | `TelegraphService.download_for_candidate()`，登记 artifact 与来源留痕 |

### 6.3 页数门禁

抓到的张数与 gdata `filecount` 不一致时（预览页被拆成 `Page-1`/`Page-2`，或图床缺图）**不发布残本**：任务置 `TELEGRAPH_PAGE_COUNT_MISMATCH`，候选走既有 `NEEDS_INFO` 通道，提示「预览页只有 N/M 页」；人工补第二个链接后重新入队复用同一任务。

此门禁**只对预览页生效**。种子的 `fsize` 与 `filesize` 天然不等（§3.1），不设此门禁。

### 6.4 来源留痕

采用 ComicInfo 加数据库，不改文件名（避免日后用原档替换预览版时破坏书库索引）：

- `download_jobs.details_json` 记录页面 URL、图床、张数、总字节、平均宽度。
- 写入 `metadata_values`：`field_name='ScanInformation'`、`value_source='TELEGRAPH'`、值形如 `TELEGRAPH_PREVIEW w1280 15p 7.5MiB`。`build_comicinfo_xml` 增加 `scan_information` 参数输出 `<ScanInformation>`，由现成的 `_metadata_lookup` 带出，不改调用链签名。
- 种子与 Telegram 来源同样写一条 `ScanInformation`（`EH_TORRENT` / `TELEGRAM`），便于日后筛出该回填的条目。

## 7. 摄取侧改动

`CandidateIngestor._parse_message` 目前只对 `text` 做正则，必须增加 `entities` / `caption_entities` 中 `text_link` 的 `url` 提取——ExHentai 链接是明文所以现在能抓到，预览链接是超链接所以现在抓不到。

同时把「只有预览链接」的消息纳入候选（目前判为「未包含图片预览、ExHentai 链接或压缩包附件」而忽略）。页面宿主白名单仅 `telegra.ph`、`graph.org`。

## 8. 安全门禁

预览页图片 URL 来自不可信的第三方内容：

- 仅 `https`；DNS 解析后拒绝回环、私网、链路本地与 IPv6 ULA（SSRF）。
- 重定向上限 3 跳，每跳复检；非 http(s) scheme 直接拒。
- 单图上限 20 MiB，单本上限 400 张 / 1 GiB，单本总超时 600 秒。
- 每张必须通过 `app/archive/safety.py` 的图片魔数校验（已支持 jpg/png/webp/gif/bmp/avif/jxl）；SVG、HTML、零字节一律拒绝。
- 独立 `httpx.AsyncClient`，不携带任何 Cookie 或 Token。

凭据面：

- .torrent 文件含 passkey，按 §5.2 处理。
- qBittorrent 用户名密码加密保存在 `data/private/`，不回显、不入日志。
- 种子内容仍要过既有归档安全门禁（路径穿越、成员数、解压大小、压缩率、魔数），来源可信度不构成豁免。

## 9. 数据模型变更

新增迁移 `app/db/migrations/010_download_sources.sql`：

```sql
ALTER TABLE candidates ADD COLUMN preview_url TEXT;
ALTER TABLE candidates ADD COLUMN torrent_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN torrent_hash TEXT;
ALTER TABLE source_messages ADD COLUMN preview_urls_json TEXT;
```

`save_candidate_message` 按 `ex_gid` 的既有写法回填 `preview_url`（为空则写、已有不覆盖）。`torrent_count` / `torrent_hash` 在 gdata 补齐元数据时写入，选路直接读候选行，不在选路时打网络请求。`ParsedSourceMessage` 增加 `preview_urls: tuple[str, ...]`。

种子进度不新增表，落在 `download_jobs.details_json`。qBittorrent 连接配置复用 `archive_settings` 键值表。

## 10. 状态与错误码

新增 provider：`EH_TORRENT`、`TELEGRAPH`。新增任务状态：`WAITING_TORRENT`（属 `OPEN_DOWNLOAD_STATES`，不属 `ACTIVE_DOWNLOAD_STATES`，可取消、不可暂停）。

| 错误码 | 含义 | 可重试 |
|--------|------|--------|
| `TORRENT_CLIENT_NOT_CONFIG` | 未登记 qBittorrent | 否 |
| `TORRENT_CLIENT_UNREACHABLE` | 客户端不可达 | 是 |
| `TORRENT_CLIENT_AUTH` | 客户端认证失败 | 否 |
| `TORRENT_NOT_AVAILABLE` | 画廊无种子 | 否 |
| `TORRENT_FILE_FETCH_FAILED` | 取 .torrent 失败（含 Cookie 失效） | 是 |
| `TORRENT_FILE_INVALID` | infohash 与 gdata 不符或 bencode 无法解析 | 否 |
| `TORRENT_PUSH_REJECTED` | 客户端拒绝加种 | 是 |
| `TORRENT_VANISHED` | 客户端里已无该 hash | 是 |
| `TORRENT_CONTENT_UNREACHABLE` | 读不到保存目录 | 否 |
| `TORRENT_CONTENT_UNEXPECTED` | 内容既非压缩包也非图片目录 | 否 |
| `TELEGRAPH_PAGE_UNREACHABLE` | 预览页请求失败 | 是 |
| `TELEGRAPH_PAGE_PARSE` | 页面结构无法解析 | 是 |
| `TELEGRAPH_NO_IMAGES` | 页面无图片 | 否 |
| `TELEGRAPH_IMAGE_BLOCKED` | 未通过安全门禁 | 否 |
| `TELEGRAPH_IMAGE_FAILED` | 图片抓取或校验失败 | 是 |
| `TELEGRAPH_LIMIT_EXCEEDED` | 超出张数、单图或总量上限 | 否 |
| `TELEGRAPH_PAGE_COUNT_MISMATCH` | 张数与 gdata `filecount` 不一致 | 是 |

`DownloadService._claim_pending_job_sync` 现在把 provider 写死为 `IN (?, ?)`，改为按 `SUPPORTED_PROVIDERS` 展开占位符，并以测试锁定——不改会静默漏领新 provider 的任务。

## 11. 配置项

| 变量 | 默认值 |
|------|--------|
| `TORRENT_ENABLED` | `true` |
| `TORRENT_CLIENT_URL` | 空（未配置即跳过该级） |
| `TORRENT_SAVE_PATH` | 空（客户端视角） |
| `TORRENT_LOCAL_SAVE_PATH` | 空（EhBot 视角） |
| `TORRENT_CATEGORY` | `ehbot` |
| `TORRENT_KEEP_SEEDING` | `true` |
| `TORRENT_POLL_SECONDS` | `15` |
| `TELEGRAPH_ENABLED` | `true` |
| `TELEGRAPH_CONCURRENCY` | `3` |
| `TELEGRAPH_MAX_IMAGES` | `400` |
| `TELEGRAPH_MAX_IMAGE_BYTES` | `20971520` |
| `TELEGRAPH_MAX_TOTAL_BYTES` | `1073741824` |
| `TELEGRAPH_TIMEOUT_SECONDS` | `600` |
| `TELEGRAPH_REQUIRE_FILECOUNT_MATCH` | `true` |

## 12. 界面改动

- 「下载任务」页：`WAITING_TORRENT` 行展示进度百分比、`num_seeds`、速度、ETA 与停滞时长；提供「改用预览图源」「用 Archive Download」「取消」。
- 审核详情页：手动来源按钮增至三个——「用种子取原档」「用 Archive Download 取原档」「用预览页下载」，沿用现有 CSRF 与 `_download_action` 写法。
- 「归档设置」页：新增 qBittorrent 区块（地址、凭据、分类、两个保存路径、连通性测试按钮），凭据不回显。

## 13. 测试方案

单元：选种打分（排除 resample、fsize 逼近、added 兜底）、bencode infohash 计算与比对、`torrents/add` 请求构造（multipart 文件字段、`savepath`、`category`、`autoTMM=false` 必须在场）、加种响应分支（`Ok.` / `415` / `403` 重登一次 / 其他非 2xx / 重复加种仍算成功）、qBittorrent 状态映射（`stalledDL`/`metaDL`/`error`/做种态）、Telegraph 节点树遍历与保序去重、相对 `/file/` 补全、`/embed/` 跳过、HTML 降级、entity 与明文混排提取、宿主白名单、SSRF 判定、三类上限、魔数拒绝 SVG 与 HTML、零填充命名。

集成（`httpx.MockTransport` 假 qBittorrent 与假图床）：

- 附件超限 → 有种子 → 走 EH_TORRENT，不再自动走 EXHENTAI。
- 推种成功进 `WAITING_TORRENT`，下载工作器不领取该行。
- 单文件 zip 完成 → 直接登记 artifact → CBZ 落地；目录完成 → 打包后 CBZ 落地。
- 硬链接/复制后原文件仍在保存目录（做种未被破坏）。
- 卡种（`num_seeds=0`）不改状态、不自动降级，手动「改用预览图源」后产生 TELEGRAPH 任务且幂等。
- 重启后按 hash 重挂轮询；hash 消失置 `TORRENT_VANISHED`。
- infohash 不符拒绝推种。
- 预览页页数不符 → 候选进 `NEEDS_INFO` 且不发布；防盗链 403 首次失败、带 Referer 重试成功。
- CBZ 内 ComicInfo 含对应 `ScanInformation`。

**需要有意修改的既有断言**：`tests/integration/test_review_actions.py:226` 断言无附件候选自动产生 `provider == "EXHENTAI"`。Archive Download 手动化后该断言必须改为 `EH_TORRENT` 或 `TELEGRAPH`，属方案要求的行为变更，会在 `progress.md` 记录理由。

真实网络用例按 `tests/integration/test_seven_zip_real.py` 的既有约定默认 skip。

## 14. 交付顺序

1. 本方案落档，`task_plan.md` 登记阶段，`findings.md` 记录实测结论，`DEVELOPMENT_PLAN.md` 3.2 加指向本方案的边界变更批注。
2. 迁移与摄取：entity 提取、`preview_url` 落库、纯预览消息纳入候选、gdata 落 `torrent_count`/`torrent_hash`。
3. `app/telegraph/`：客户端、门禁、抓取、打包（打包器供种子目录收货复用）。
4. `app/torrent/`：选种、取 .torrent、bencode infohash、qBittorrent 适配、轮询任务、收货。
5. 下载队列接线：provider 列表修复、`WAITING_TORRENT` 状态、新错误码、四级选路、Archive Download 手动化。
6. 界面：下载任务页进度与手动动作、审核页三按钮、归档设置页 qBittorrent 区块。
7. ComicInfo `ScanInformation`、配置项、README 小节。
8. 全量 `pytest`；用样本中的真实数据做带网络的手工验证——gid 4108964 走种子全程，gid 1655718（`torrentcount=0`）走预览页，并把耗时、体积、做种状态记入 `progress.md`。

## 15. 暂不承诺

- 不引入 MTProto / Telethon。它是从 Telegram 直取超限原档的唯一途径，但需要 `api_id`、`api_hash` 与用户会话，属独立产品决策。
- 不在主进程内跑 BitTorrent，不引入 libtorrent。
- 不用裸磁力链替代 .torrent（EH tracker 需要 passkey）。
- 不解码第三方图床路径中的 `file_id` 去调用本项目 Bot 的 `getFile`：跨 Bot 的 `file_id` 对 `getFile` 不可用。
- 不对图片做任何重编码、放大或格式转换。
- 不处理 `t.me/c/...` 频道消息链接。
- 不自动做种删除或做种比例管理，交由 qBittorrent 自身策略。
