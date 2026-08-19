# EhBot

EhBot 是面向 Telegram 漫画来源的本地审核与 CBZ 归档服务。当前仓库完成开发方案的阶段 1：基础工程、SQLite 持久化、管理员登录、Web 工作台、健康检查和 Docker 部署。

Telegram 与 ExHentai 尚未接入，工作台会明确显示为“尚未配置”。当前阶段不会发起任何 TG/EX 网络请求。

## 本地开发

要求 Python 3.12 和 `uv`。

```powershell
python -m uv sync
python scripts/create_secrets.py
$env:APP_SECRET_KEY_FILE = (Resolve-Path secrets/app_secret_key)
$env:ADMIN_PASSWORD_HASH_FILE = (Resolve-Path secrets/admin_password_hash)
python -m uv run uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000`。存活和就绪检查分别为 `/healthz` 与 `/readyz`。

## Docker

先生成本地秘密文件，再启动服务：

```powershell
python -m uv run python scripts/create_secrets.py
Copy-Item .env.example .env
docker compose up --build -d
```

默认访问地址为 `http://127.0.0.1:8080`。数据、书库和工作目录通过 Compose 绑定到宿主机；重新创建容器不会删除这些目录。

## 验证

```powershell
python -m uv run pytest
docker compose config
```

所有秘密文件、`.env`、数据库和运行目录均被 Git 忽略。不要将 Telegram session、Token、API Hash 或 ExHentai Cookie 写入仓库或日志。
