# EhBot

把 Telegram 频道里的漫画整理成规范的 CBZ 归档库。

服务盯着你指定的 Telegram 来源，把消息解析成**候选**；你在网页上审核，通过之后它才开始下载，
下载完解压、按 ExHentai 元数据补齐信息、打包成带 `ComicInfo.xml` 的 CBZ，按模板放进书库目录，
交给 Komga、Kavita 这类阅读器接管。

**范围止于归档。** 下载 → 转换成目标归档格式，外加少量便于操作的管理项（列表、批量重新打包、
移除、重新下载、改归档路径）。它不是书库管理器：没有阅读器、没有书架分组，也不会扫描不是本服务
下载的文件——那些交给下游工具。

主要能力：

- **审核先行**：任何东西在你点通过之前都不会下载。支持批量通过/驳回、自动审批规则（可对历史候选试跑）。
- **四级下载选路**：Telegram Bot → Telegram 用户账户（绕过 20 MB 上限）→ ExHentai 种子 → telegra.ph 预览页，
  按「画质优先、成本其次」自动降级。消耗 GP 的 ExHentai Archive Download 永不自动触发，只作手动按钮。
- **元数据以 ExHentai 为唯一权威源**，标签自动汉化，你手改过的字段可以锁定，不被后续刮削覆盖。
- **归档路径可控**：按 `{category}/{artist}/{title}` 模板生成，也可在作品详情页为单本书指定目录与文件名。
- **安全默认**：解压前校验路径穿越、解压炸弹、图片魔数；凭据加密存储，页面不回显、日志不记录；
  移除记录时**默认不删文件**。

界面按「工作台 / 候选 / 活动 / 已下载 / 设置」五个域组织，单容器、单管理员、SQLite，无需外部数据库。

## 部署（推荐 Docker）

镜像已发布到 Docker Hub，部署机不需要源码，也不需要 Python 或构建工具链。

**1. 建一个目录，放入 `compose.yaml`：**

```yaml
services:
  ehbot:
    image: hsmk/ehbot:latest
    container_name: ehbot
    init: true
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: "Asia/Shanghai"
    volumes:
      # 数据库、会话密钥、加密密码库、7-Zip 安装。丢了就要重录所有凭据，务必保留。
      - ./data:/app/data
      # 打包好的 CBZ 落点，交给 Komga/Kavita 读取。
      - ./library:/library
      # 解压工作目录；若用 qBittorrent 下载，这里要能读到它的完成目录。
      - ./work:/work
```

**2. 启动并取初始密码：**

```powershell
docker compose up -d
docker compose logs
```

**3. 打开 `http://127.0.0.1:8080`**，用户名 `admin`，密码是日志顶部横幅里那串一次性密码
（同时备份在 `data/bootstrap_admin_password`）。登录后必须先改密码才能用其他页面；改完该文件即被删除。
只要没改过，每次重启都会重新生成一串新的。

**4. 在「设置 → 外部连接」填入 Telegram Bot Token 与 ExHentai Cookie**，再到「设置 → 来源规则」
添加要监听的频道。凭据存在 `data/private/` 下，页面只显示连接状态，不回显内容。

会话签名密钥首次启动自动生成，不用手工创建任何秘密文件。仓库里的 `compose.deploy.yaml` 是一份
带完整环境变量注释的参考，可直接抄。

### Windows 上的注意事项

**Windows 请务必用 Docker，不要在宿主机上直接跑。** 归档环节依赖官方发布的 Linux 版 7-Zip 静态构建
（服务自己下载到 `data/tools` 并校验 SHA-256），Windows 上没有这个形式的二进制，因此宿主机运行时
RAR / 7z 解压能力缺失。容器里没有这个问题。

- 安装 Docker Desktop 并启用 WSL 2 后端。
- **把绑定目录放在 WSL 文件系统里**（例如在 `\\wsl$\...` 下建目录），而不是 `C:\Users\...`。
  跨 `/mnt/c` 访问会让解压和打包慢一个量级，`data/` 里的 SQLite 也可能因文件锁差异出问题。
- Windows 上不需要设置 UID/GID。Linux 宿主若当前用户不是 `1000:1000`，则要在 `.env` 里改
  `EHBOT_UID` / `EHBOT_GID`，否则绑定目录不可写。
- 用 PowerShell 跑上面的命令即可，路径分隔符照写 `/`。

### 其他部署方式

从源码构建（需要 Docker 引擎）：

```powershell
git clone https://github.com/hatsusakuramiku/EhBot.git
cd EhBot
Copy-Item .env.example .env
docker compose up --build -d
```

不用容器直接跑（仅建议在 Linux 上，或用于开发）：需要 Python 3.12 与 `uv`。

```powershell
python -m uv sync
python -m uv run uvicorn app.main:app --reload
```

访问 `http://127.0.0.1:8000`。存活与就绪检查分别是 `/healthz` 与 `/readyz`。

### 升级

```powershell
docker compose pull
docker compose up -d
```

数据库迁移在启动时自动执行，只追加不回滚。`data/` 与 `library/` 是绑定目录，重建容器不会动它们。

## 文档

| 文件 | 内容 |
|------|------|
| [docs/USAGE.md](docs/USAGE.md) | 逐项功能说明：五个域的页面、下载选路、用户账户登录、qBittorrent、归档处理、缩略图代理、全部环境变量 |
| [compose.deploy.yaml](compose.deploy.yaml) | 生产部署参考，环境变量逐条带注释 |
| [.env.example](.env.example) | 环境变量默认值 |
| [AgentHelp/](AgentHelp/) | 开发者文档：需求规格、开发计划、实现日志、设计方案 |

## 许可证

本项目以 MIT 许可证发布，详见 [LICENSE](LICENSE)。

本仓库只提供自托管的归档工具，不附带任何凭据、Cookie 或版权内容。使用者需自行遵守 Telegram、
E-Hentai 的服务条款以及当地法律法规，并对所归档的内容自负其责。