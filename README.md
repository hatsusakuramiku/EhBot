# EhBot

EhBot 是面向 Telegram 漫画来源的本地审核与 CBZ 归档服务。当前仓库已完成基础工程、SQLite 持久化、管理员登录、响应式 Web 工作台、健康检查和 Docker 部署，并提供外部连接管理。

Web UI 支持填写一个 Bot Token 接入 Telegram Bot API，服务会通过 `getMe` 校验身份并使用 `getUpdates` 持久化接收更新；也支持填写 `ipb_member_id`、`ipb_pass_hash` 和 `igneous` 校验 ExHentai Cookie 会话。当前已支持离线 Update 解析、来源白名单、压缩格式与附件大小规则、`NEEDS_INFO` 待补充队列、确定性消息归并和只读候选页面；尚未实现人工通过/驳回、Telegram 媒体下载和 ExHentai 归档下载。

## 本地开发

要求 Python 3.12 和 `uv`。

```powershell
python -m uv sync
python -m uv run python scripts/create_secrets.py
$env:APP_SECRET_KEY_FILE = (Resolve-Path secrets/app_secret_key)
python -m uv run uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000`。存活和就绪检查分别为 `/healthz` 与 `/readyz`。

首次启动时，系统会创建 `admin` 管理员并生成临时密码。为避免密码进入日志，命令行只提示私有文件路径；临时密码保存在 `data/bootstrap_admin_password`。使用该密码登录后必须立即修改密码，成功后临时文件会被删除。只要密码尚未在 Web UI 修改，每次重启都会轮换临时密码。

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

## 验证

```powershell
python -m uv run pytest
docker compose config
```

所有秘密文件、`.env`、数据库和运行目录均被 Git 忽略。不要将 Telegram session、Token、API Hash 或 ExHentai Cookie 写入仓库或日志。
