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

先生成本地秘密文件，再启动服务：

```powershell
python -m uv run python scripts/create_secrets.py
Copy-Item .env.example .env
docker compose up --build -d
```

默认访问地址为 `http://127.0.0.1:8080`。数据、书库和工作目录通过 Compose 绑定到宿主机；重新创建容器不会删除这些目录。

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
- **取消**：任务终止，候选退回「待审核」队列，不会丢失。已完成的任务不可取消。

**Telegram 单文件 20 MB 上限**：Telegram Bot API 拒绝下载超过 20 MB 的文件（`getFile` 返回 `file is too big`）。这是官方接口限制，与代理、超时或重试无关，任务会以 `TELEGRAM_FILE_TOO_BIG` 失败。可选方案：改用 ExHentai 源下载同一本，或请上传者分卷重发。

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
