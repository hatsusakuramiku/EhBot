# EhBot

EhBot 是面向 Telegram 漫画来源的本地审核与 CBZ 归档服务。当前仓库已完成基础工程、SQLite 持久化、管理员登录、响应式 Web 工作台、健康检查和 Docker 部署，并提供外部连接管理。

Web UI 支持填写一个 Bot Token 接入 Telegram Bot API，服务会通过 `getMe` 校验身份并使用 `getUpdates` 持久化接收更新；也支持填写 `ipb_member_id`、`ipb_pass_hash` 和 `igneous` 校验 ExHentai Cookie 会话。当前已支持离线 Update 解析、来源白名单、压缩格式与附件大小规则、`NEEDS_INFO` 待补充队列、确定性消息归并、人工批量通过/驳回、自动审批规则、Telegram 与 ExHentai 下载，以及可扩展的归档处理与 CBZ 发布。

## 本地开发

要求 Python 3.12 和 `uv`。

```powershell
python -m uv sync
python -m uv run python scripts/create_secrets.py
$env:APP_SECRET_KEY_FILE = (Resolve-Path secrets/app_secret_key)
python -m uv run uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000`。存活和就绪检查分别为 `/healthz` 与 `/readyz`。

首次启动时，系统会创建 `admin` 管理员并生成临时密码，密码以横幅形式直接打印到控制台，同时写入 `data/bootstrap_admin_password` 作为备份（详见「首次登录」）。使用该密码登录后必须立即修改密码，成功后临时文件会被删除。只要密码尚未在 Web UI 修改，每次重启都会轮换临时密码。

Telegram Token 和 ExHentai Cookie 保存在 `data/private/` 下的私密文件中。页面只显示连接状态和已验证身份，不会回显已保存凭据。

## Docker

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

默认访问地址为 `http://127.0.0.1:8080`。数据、书库和工作目录通过 Compose 绑定到宿主机；重新创建容器不会删除这些目录。

会话签名密钥在首次启动时自动生成并保存到 `data/private/session_secret_key`，无需手工创建任何秘密文件；只有多副本共用一把密钥时才需要自行设置 `APP_SECRET_KEY`。请务必保留 `data/` 目录：删除它意味着会话密钥、密码库与所有凭据全部丢失。

Linux 默认以宿主 UID/GID `1000:1000` 运行。若当前用户不同，请在 `.env` 中将 `EHBOT_UID`、`EHBOT_GID` 改为当前普通用户的数值，确保绑定目录可写。

## 标签翻译

服务启动时会从 EhTagTranslation 的 GitHub Release 下载 `db.text.json.gz`，缓存到 `data/ehtag_db.json`，并在内存中建立 `namespace:tag` 索引，用于把 ExHentai 官方 API 返回的英文元数据翻译成中文。

- 每次启动先用 `ETag` 做条件请求，未变更则直接使用本地缓存；距上次检查不足 24 小时时完全跳过网络请求。
- 网络不可用时自动降级到本地缓存；没有缓存时跳过翻译，元数据保留英文原文。
- 中文值写入 `Title`、`Artist`、`Group` 等主字段，英文原文保存在对应的 `*Raw` 字段，审核页可展开查看。
- 如需关闭该功能（例如离线部署），在 `.env` 中设置 `TAG_TRANSLATION_ENABLED=false`。

## 下载队列

审核通过的候选会进入下载队列，「下载任务」页展示待处理、进行中、已暂停和失败的任务（失败任务不会消失，以便重试）。

- **重试**：复用同一任务行，不会产生重复作业，尝试次数会累加；候选会从失败状态回到已通过。因永久原因失败的任务（例如文件超过 Bot API 上限）不提供重试按钮。
- **暂停 / 继续**：只对尚未开始的任务生效。暂停后工作器不会领取它，点击继续即重新排队。已在下载中的任务无法中途挂起，只能取消。
- **取消**：任务终止，候选退回「待审核」队列，不会丢失。已完成的任务不可取消。取消停放中的种子任务会同时从 qBittorrent 移除该种（不删文件）。
- **等待种子**：`WAITING_TORRENT` 行额外展示进度百分比、做种者数、速度、ETA 与停滞时长，并提供切换来源的动作，详见下文。

**Telegram 单文件 20 MB 上限**：Telegram Bot API 拒绝下载超过 20 MB 的文件（`getFile` 返回 `file is too big`）。这是官方接口限制，与代理、超时或重试无关。因此审核通过时不会把超限附件交给 Telegram 源，见下节的降级链路。

## 下载来源与降级链路

审核通过时按「画质优先、成本其次」自动选路，元数据始终以 ExHentai 画廊为唯一权威源：

| 顺序 | 来源 | 画质 | 触发条件 |
|------|------|------|----------|
| 1 | `TELEGRAM` | 原档 | 有压缩附件且不超过 20 MB |
| 2 | `EH_TORRENT` | 原档，且免费 | gdata 报告有种子且已登记 qBittorrent |
| 3 | `TELEGRAPH` | 1280px 重编码，约为原档 5%–10% | 消息带 `telegra.ph` / `graph.org` 预览页链接 |

- **种子是超限本子的首选原档来源**：内容是上传者的原始压缩包，不消耗 GP，也不受 20 MB 限制。
- **ExHentai Archive Download 不参与自动选路**：它消耗 GP，属于操作者决策，只在审核详情页提供「用 Archive Download 取原档」按钮。四条来源都不可用时，审核会直接报「没有可用的下载来源」而不静默花 GP。
- **预览页是兜底，不是默认**。页数完整（实测 22/22、15/15、78/78）且免 Cookie，但成品是统一缩到宽 1280 的重编码版本，只作阅读级替代。
- 预览页链接通常是超链接，URL 只存在于消息的 `text_link` entity 里；只带预览链接的消息也会被纳入候选。
- 抓到的张数与画廊 `filecount` 不一致时**不发布残本**：任务以 `TELEGRAPH_PAGE_COUNT_MISMATCH` 失败，候选退回「需要补充信息」并显示「预览页只有 N/M 页」，补齐链接后重试复用同一任务。设 `TELEGRAPH_REQUIRE_FILECOUNT_MATCH=false` 可关闭该门禁。
- 预览页图片来自频道自建的第三方图床，因此抓取前后有独立门禁：仅 https、DNS 解析后拒绝回环与内网、重定向逐跳复检（上限 3 跳）、单图与单本字节和张数上限、图片魔数校验（拒绝 SVG 与 HTML），且使用不携带任何 Cookie 的独立 HTTP 客户端。防盗链 403 时会带 `Referer: https://telegra.ph/` 重试一次。
- 来源等级写入 `ComicInfo.xml` 的 `<ScanInformation>`（形如 `TELEGRAPH_PREVIEW w1280 15p 7.5MiB`、`EH_TORRENT original 121.0MiB`）与任务详情，**不改文件名**，便于日后用原档替换而不破坏书库索引。

## EH 种子与 qBittorrent

种子下载全程交给外部 qBittorrent，**主进程不跑 P2P、不引入 libtorrent**，低配目标不受影响。在「归档设置」页登记地址、凭据、分类与两个保存目录后即可使用，凭据加密保存在 `data/private/` 且页面不回显。

- **选种**：gdata 响应里已经带 `torrentcount` 与 `torrents`，发现种子不需额外请求。多条时排除 `resample` 重采样版，再取 `fsize` 最接近画廊 `filesize` 者，最后取最新。
- **取 .torrent**：`torrents[].hash` 只是 infohash，文件需要登录态从 `gallerytorrents.php` 取，链接一律从页面解析而不硬编码。本地用 bencode 算 infohash 与 gdata 比对，不一致直接 `TORRENT_FILE_INVALID`，不推给客户端。
- **.torrent 属于凭据**：它的 announce 内含账号 passkey，只停留在工作目录，不进书库、不入日志、不入审计。也不用裸磁力链替代：EH tracker 需要 passkey。
- **推种**：走 `torrents/add`，显式下发 `savepath` 与 `category` 并关掉 `autoTMM`（否则分类规则会覆盖保存路径，EhBot 会去错目录找文件），不传 `root_folder` 以保留种子结构。不用监视文件夹，不碰客户端配置。
- **兼容新旧 WebAPI**：登录成功在部分构建与反代下是 `204` 而非 `200 Ok.`，加种在 WebAPI 2.11+ 返回 JSON 报告而非 `Ok.`，两者均按成功处理；仅当报告里无任何成功且 `failure_count > 0` 时判 `TORRENT_PUSH_REJECTED`。
- **重复加种不是错误，但会提示**：客户端已持有该 infohash 时（新版回 `409`）任务照常停放，页面标注「该种子已存在于 qBittorrent」——因为实际干活的是别人创建的条目，其保存目录与分类未必是刚下发的那些，收货可能找错目录。
- **存活与重启**：推种成功后任务进 `WAITING_TORRENT`，不占用下载并发名额，由独立轮询任务每 `TORRENT_POLL_SECONDS` 秒拉一次进度。停放任务每轮从数据库读取，因此重启后自动按 hash 重新挂上；客户端里已无该 hash 则置 `TORRENT_VANISHED`（可重试）。
- **卡种不是错误**：`stalledDL` / `metaDL` 且做种者为 0 时，任务继续等待并在「下载任务」页显示「无做种者，已停滞 N 分钟」，**不自动失败、也不自动降级**。降到预览级或花 GP 都是操作者决策，页面提供「改用预览图源」「用 Archive Download」「取消」三个动作，切源与取消会从客户端移除该种。
- **做种可见且可停**：完成后仍在做种的任务继续留在「下载任务」页（其他源完成即消失），显示上传速率并提供「停止做种」；停止只从客户端移除条目，不删已归档文件。因为做种仍在占用带宽与磁盘，它就不应该从页面上消失。
- **页面自动刷新**：进度由后台轮询写入，因此存在下载中或做种中的任务时，「下载任务」页每 `TORRENT_POLL_SECONDS` 秒自刷；队列空闲时不刷。轮询每轮输出 `torrent_progress` 日志（状态、百分比、做种者、速率），便于不开页面也能跟进度。
- **收货硬链接优先，其次复制，绞不移动**：移动会破坏做种。默认 `TORRENT_KEEP_SEEDING=true`，完成后继续做种；关掉时也只从客户端移除条目，不删文件。单文件压缩包直接登记为 artifact，目录则校验为图片后按 `ZIP_STORED` 打包，之后完全复用现有归档流水线与安全门禁。
- **下载后自动打包（默认关闭）**：开启后收货完成即自动入队归档流水线，无需手动点「打包」。默认关闭是因为打包意味着发布到书库，不应成为下载完成的副作用。**开启时必须填写「保存目录（EhBot 视角）」**，且保存时会验证该目录可列出（仅 `is_dir()` 不够：无读权限的挂载也能通过），否则无人看着的打包会在几小时后才失败。打包失败不会把下载标成失败：artifact 已登记，仍可手动转换，日志记 `TORRENT_AUTO_PACK_FAILED`。
- **两个保存目录**：`保存目录（客户端视角）` 随加种请求下发，`保存目录（EhBot 视角）` 用于读取。客户端在其他容器或 NAS 时两边路径不同，因此分开登记；保存时就校验后者可读，而不是等三个小时后才发现写错。
- 置空客户端地址或设 `TORRENT_ENABLED=false` 即跳过这一级，选路直接降到预览页。

## 归档处理

下载完成后的压缩包经一条固定流水线发布为 CBZ：分卷检查 -> 密码尝试 -> 安全校验 -> 解压或流式复制 -> 同一后端打包 -> 原子发布。

- ZIP/CBZ 使用内置 `zipfile-default` profile，成员直接流式写入 CBZ，不需要先全量解压；图片以 `ZIP_STORED` 写入，不重复压缩。
- RAR、7Z、分卷包和 `zipfile` 无法打开的加密 ZIP 使用 `7zz-default` profile，通过受控子进程调用；主进程不加载第三方 DLL。
- 7-Zip 不依赖发行版包：服务首次启动时从 `ip7z/7zip` 官方 GitHub Release 拉取固定版本的 `.tar.xz`，校验 SHA-256 后解包到 `data/tools/7zip/<版本>/`，优先使用静态链接的 `7zzs`，因此 slim 镜像无需额外运行库。存档只用 Python 内建 `tarfile` 解开，不存在「解压工具需要解压工具」的自举问题。
- 安装目录按版号隔离且幂等：已有可用二进制时直接复用，校验失败的下载会被丢弃且不影响现有安装。设置 `ARCHIVE_TOOLCHAIN_AUTO_INSTALL=false` 可关闭自动拉取（离线部署），此时可用「归档设置」页的下载按钮、预先运行 `python -m scripts.install_seven_zip --data-path /app/data`，或直接填写宿主自带的 `7zz`/`7z` 路径（PATH 和 Windows 默认安装目录作为回退）。
- 官方未发布二进制的平台（例如 Windows 开发机）不会尝试下载，页面会说明需要手动填写路径；归档功能在 Linux/Docker 上不受影响。
- 全量解压前先校验路径穿越、绝对路径、符号链接、嵌套压缩包、成员数、解压大小、异常压缩率、目录层级和图片魔数，任何超限直接失败。
- 分卷不完整时任务进入「待补分卷」，密码全部尝试失败时进入「待补密码」；补齐后重新入队会复用同一任务，不会产生重复作业。
- 密码库条目加密保存，页面不回显密码，日志和审计也不记录密码本身；主密钥保存在 `data/private/archive_password_key`。
- 在「归档设置」页面可调整安全限制、是否保留原始压缩包、工具 profile 的可执行文件/超时/启用状态和密码库条目。页面只能选择已登记的 profile，不接受任意命令行。
- 书库目录和工作目录也在该页修改，必须是绝对路径且可写（不存在时自动创建），保存后下一个任务即生效，无需重启；清空输入框即恢复环境变量默认值。数据目录存放设置数据库本身，只能通过 `DATA_PATH` 环境变量指定。

## 封面缩略图

审核列表的封面不直连图床，而是由本机代理后再交给浏览器：URL 形如 `/api/v1/thumbnails/<hash>`，`hash` 是「来源 URL + 变体」的 SHA-256。

- **不回传上游地址**：让 `<img>` 指向 ExHentai 的 CDN 等于把本部署渲染过的每一张封面都告诉那台主机，而且它一拒绝防盗链页面就全线破图。
- **不是开放代理**：接口只接受 hash，不接受任何调用方给的 URL。抓取画廊元数据时才会在同一个事务里写入 `candidates.thumb_url` 和一条 `PENDING` 的 `thumbnails` 记录——这条记录是唯一的准入口，没有它的 hash 一律不抓。
- **可以永久缓存**：hash 覆盖了来源身份，同一 URL 换不出别的图，因此响应带 `ETag` 与 `Cache-Control: immutable`，二次请求靠 `If-None-Match` 换 `304`。
- **出站字节全部由自己产出**：抓到的图先过魔数与像素上限，再由 Pillow 解码并统一重编码为 WebP（长边上限 512，只缩不放）。上游用 `200` 送回 HTML 错误页的情况会被魔数门禁挡住，不会存成封面。
- **抓取受控**：与预览页共用同一套 SSRF 门禁（仅 https、解析后拒绝回环与内网），同一 hash 的并发请求合并为一次抓取，全局出站并发上限 4，防盗链 403/404 时带 `Referer: https://exhentai.org/` 重试一次。
- **失败返回占位图而不是 404**：`<img>` 的 404 会渲染成破图图标且无法用样式补救，因此失败时返回一张真实的占位图并配 `X-Thumbnail-State: failed` 头与短缓存。
- 设 `THUMBNAILS_ENABLED=false` 即完全关闭该服务，列表渲染占位图，不向图床发出任何请求。

## 首次登录

服务首次启动时会生成一个一次性的管理员密码，并直接打印到控制台：

```
docker compose logs
```

- 用户名固定为 `admin`，密码以横幅形式打印在启动日志顶部，不需要去翻数据目录。
- 同时会写入 `data/bootstrap_admin_password` 作为备份（仅当前用户可读）。
- 登录后必须先修改密码，才能访问其他页面；修改后该文件被删除，控制台也不再打印。
- 若密码从未被修改，每次重启都会重新生成并重新打印。

## 验证

```powershell
python -m uv run pytest
docker compose config
```

Windows 上无法验证托管的 7-Zip 安装（官方只为 Linux/macOS 发布该形式的二进制），这部分用 Docker 验证（需已启动的 Docker 引擎）：

```powershell
python scripts/verify_docker_linux.py --offline --suite --build
```

- 默认阶段：在一个不带 7-Zip 的 Linux 容器里完整验证拉取、校验、幂等安装、静态二进制可运行、RAR 支持与真实 7z 往返。
- `--offline`：断网时安装必须以 `TOOLCHAIN_DOWNLOAD_FAILED` 失败且不留下残缺文件。
- `--suite`：在 Linux 上跑全量测试，依赖由 `uv.lock` 解析。
- `--build`：构建应用镜像并在镜像内预置 7-Zip。

所有秘密文件、`.env`、数据库和运行目录均被 Git 忽略。不要将 Telegram session、Token、API Hash 或 ExHentai Cookie 写入仓库或日志。

## 许可证

本项目以 MIT 许可证发布，详见 [LICENSE](LICENSE)。

本仓库只提供自托管的归档工具，不附带任何凭据、Cookie 或版权内容。使用者需自行遵守 Telegram、E-Hentai 的服务条款以及当地法律法规，并对所归档的内容自负其责。
