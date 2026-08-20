# 归档处理扩展方案

## 1. 目标

将归档处理统一为一条可恢复、可审计、可扩展的任务流水线：

```text
下载归档 -> 分卷检查 -> 密码尝试 -> 安全测试 -> 解压处理 -> 使用同一后端打包 CBZ -> 原子发布
```

当前文档只定义方案，不代表功能已经实现。

## 2. 处理边界

`ConversionService` 只负责调度一个 `ArchiveProcessor`，不直接判断 ZIP/RAR/7Z 的细节。`ArchiveProcessor` 根据任务快照选择一个已登记的 `ArchiveBackend`，并依次调用：

1. `inspect`：识别格式、列出分卷和目录清单。
2. `test_password`：使用空密码或密码库条目验证可读性。
3. `validate_safety`：检查路径、文件数量、解压大小、压缩比和文件类型。
4. `extract`：将内容解压到任务专属临时目录。
5. `pack_cbz`：使用同一个后端从临时目录生成 CBZ，并包含 `ComicInfo.xml`。

所有中间文件使用任务专属目录；最终文件先写入 `.part`，完成后再原子重命名。

## 3. 后端接口

```python
class ArchiveBackend(Protocol):
    def inspect(self, source: Path, password: str | None) -> ArchiveManifest: ...
    def test_password(self, source: Path, password: str | None) -> None: ...
    def validate_safety(self, manifest: ArchiveManifest) -> None: ...
    def extract(self, source: Path, destination: Path, password: str | None) -> None: ...
    def pack_cbz(self, source_directory: Path, destination: Path) -> None: ...
```

首批后端：

- `ZipfileBackend`：普通 ZIP/CBZ，使用 Python 标准库。
- `SevenZipBackend`：RAR、7Z、分卷包和 `zipfile` 无法处理的加密 ZIP，使用受控 `7zz` 子进程。
- `ExternalToolBackend`：预留给特定格式的 CLI/DLL 桥接程序。

主进程不直接加载不受控 DLL。DLL 型工具通过独立桥接子进程调用，主进程只通过受限参数和结构化结果通信；工具崩溃或超时只影响当前任务。

## 4. 工具配置

任务只保存 `backend` 和 `tool_profile`，不接受用户提交的任意命令行：

```json
{
  "backend": "seven_zip",
  "tool_profile": "7zz-default",
  "source_format": "rar"
}
```

工具配置由管理员预先登记，包括可执行文件/桥接程序路径、支持格式、超时和能力标记。Web 页面只允许选择已登记 profile。

## 5. 分卷与密码

- 支持 `.part1.rar`、`.rar + .r00`、`.7z.001`、`.zip.001` 等常见命名。
- 分卷按候选和文件名生成 `volume_group`，缺卷进入 `WAITING_VOLUMES`。
- 首卷驱动 7zz，不拼接分卷文件。
- 密码库保存加密密文、名称、优先级和启用状态；密码内容不进入普通日志、审计或页面。
- 密码按“上次成功密码 -> 启用密码优先级 -> 空密码”尝试；全部失败进入 `WAITING_PASSWORD`。

## 6. 状态与审计

任务增加或使用以下状态：

```text
PENDING, WAITING_VOLUMES, WAITING_PASSWORD, INSPECTING,
EXTRACTING, PACKING, COMPLETED, FAILED
```

`details_json` 记录格式、分卷组、后端、工具 profile、密码条目 ID、检查结果和错误代码，但不记录密码本身。

## 7. 设置页面

新增登录后可用的设置页面，配置：

- 默认下载目录、工作目录和图库目录。
- 任务级目标目录和是否保留原始归档。
- 7zz/profile 状态。
- 文件数、解压总大小、压缩比和任务超时限制。
- 密码库条目管理。

目录必须是绝对路径，并通过允许根目录校验；任务保存生效路径快照，避免之后修改默认目录影响历史任务。

## 8. 安全门禁

在全量解压前完成：魔数校验、路径穿越检查、文件数量限制、解压大小限制、压缩比限制、临时目录隔离和超时终止。任何失败都清理临时目录和 `.part` 文件。

## 9. 暂不承诺

- 不在主进程内直接加载任意 DLL。
- 不自行实现 RAR/7Z 解码器。
- 首版只保证已登记的 7zz 版本和工具 profile；未知格式进入 `UNSUPPORTED_FORMAT`。

