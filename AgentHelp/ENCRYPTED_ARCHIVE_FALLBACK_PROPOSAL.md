# 方案：内置解压器读不了的 ZIP 回退到 7-Zip（R51 草案）

> 状态：**待运营者审阅**。审阅通过前不动产品代码。本文件回应运营者 2026-10-01 的
> 报告：「加密压缩包在密库里已配置正确的密码但是无法正确解压缩打包处理」。
> 2026-10-01 追加 **§9 附录：RAR 工具链评估**（回应「加一个 RAR 工具链」）——
> 结论是 RAR 读路径本身已可用、不建议托管 rarlab 工具链，详见 §9.0。
> 同日追加 **§10**：运营者实测镜像后又报跨挂载打包失败（`EXDEV`），修法见该节。

## 0. 结论先说

**这不是密码的问题，是后端选择的问题。** 只要那个加密 ZIP 是 7-Zip / WinRAR / Bandizip
用 **AES-256**（或 Deflate64 等非标准方法）做出来的，内置 `zipfile` 就永远打不开它，
而现在这条流水线**没有回退到已随镜像安装的 7-Zip**：正确密码会被逐个判成「密码错误」，
任务停在「待补密码 / 归档已加密，当前密码库无法打开」。同一条流水线对 ZipCrypto ZIP、
7z（含 `-mhe=on` 头部加密）都是好的——所以「密码库配置正确却打不开」这个现象只发生在
一类包上，且与密码无关。

`docs/USAGE.md:229` 本来就写着「RAR、7Z、分卷包和 `zipfile` 无法打开的加密 ZIP 使用
`7zz-default` profile」——代码没有做到文档承诺的事，这算缺陷而不是新需求。

## 1. 复现（本机，用仓库自带的 7-Zip 26.02）

同一组 3 张 JPEG 页，分别打成四种包，密码统一 `S3cret`，密码库只放这一条正确密码，
按 `ArchiveProcessor.process()` 跑：

| 包 | 正确密码在库里 | 只启用 7zz profile |
|----|----------------|--------------------|
| `zip -pS3cret`（ZipCrypto） | **OK** pages=3 password_id=1 | OK |
| `zip -mem=AES256 -pS3cret` | **FAIL** `ArchivePasswordRequired`「归档已加密，当前密码库无法打开」 | **OK** pages=3 |
| `7z -pS3cret`（仅数据加密） | OK | OK |
| `7z -pS3cret -mhe=on`（头部加密） | OK | OK |

更直白的一例：**完全没有加密**、只是用了 Deflate64 的 ZIP（`-mm=Deflate64`），
当前也会报成「归档已加密，当前密码库无法打开」，并停在「待补密码」。

复现脚本（临时目录，不写仓库）：

```bash
7zz a -tzip -mem=AES256 -pS3cret enc.zip pages/*      # 或 -mm=Deflate64
# 然后 ArchiveProcessor(profiles=ALL_PROFILES, passwords=((1, "S3cret"),)).process(enc.zip, ...)
```

## 2. 根因（两处，叠加）

### 2.1 `NotImplementedError` 被当成「密码错误」

`app/archive/backends/zip_backend.py` 的 `test_password`（83 行）、`extract`（112 行）、
`stream_pages`（158 行）都是同一个写法：

```python
except RuntimeError as exc:            # 99 行：先命中
    raise ArchivePasswordRequired() from exc
except NotImplementedError as exc:     # 101 行：永远不会执行
    raise ArchiveError("ARCHIVE_COMPRESSION_UNSUPPORTED", "……不支持的加密或压缩方式")
```

`NotImplementedError` 是 `RuntimeError` 的**子类**，所以第二个 `except` 是死代码：
「内置解压器不认识这个方法」被翻译成「密码不对」。于是 `_resolve_password()`
（`app/archive/processor.py:261`）把密码库里每一条（包括正确的那条）都判为失败，
最后抛 `ArchivePasswordRequired`；`app/conversion/service.py:1128` 捕获它并把任务置为
`CONVERSION_STATE_WAITING_PASSWORD`（页面文案「待补密码」）。运营者看到的就是
「密码明明填对了，还是说打不开」。

### 2.2 只挑一个后端，失败不回退

`ArchiveProcessor.select_profile()`（`app/archive/processor.py:64`）只要 profile 支持 zip
就优先内置 `zipfile`，`process()`（91 行）只用这一个后端跑完整条流水线，失败不会改用
7-Zip。而 Python 的 `zipfile` 只支持 Stored(0) / Deflate(8) / Bzip2(12) / LZMA(14)：
- AES-256 的 ZIP 用压缩方法 99（`compress_type == 99`），`zipfile` 直接
  `NotImplementedError: That compression method is not supported`；
- Deflate64（方法 9）同理。

7-Zip 26.02 两种都能打开（见上表第 4 列），仓库默认就装了它、`7zz-default` profile
也默认启用并声明支持 `zip`（`app/db/migrations/009_archive_processing.sql:20`）。
缺的只是「这个后端读不了就换下一个」这一步。

### 2.3 为什么测试没抓到

`tests/integration/test_seven_zip_real.py` 的加密用例只覆盖 `7z -p` / `7z -p -mhe=on`，
它们无论如何都走 7zz，触发不到 zipfile 后端；加密 ZIP 只有 ZipCrypto 这一种被覆盖，
而它恰好是 `zipfile` 支持的。**「加密 ZIP」在测试里没有被当成一个方法维度看待。**

## 3. 方案

### 3.1 新增一个「换后端」信号

`app/archive/errors.py` 加 `ArchiveBackendUnsupported(ArchiveError)`，错误码沿用
`ARCHIVE_COMPRESSION_UNSUPPORTED`（不新增面向运营者的词），语义是「**这个后端**读不了这个包，
请换一个后端」——它将只被 `ArchiveProcessor` 捕获，用来决定回退；如果没有任何后端能接，
它才以普通 `ArchiveError` 的身份向上抛（此时文案要说清楚是方法不支持、并提示启用 7-Zip）。

### 3.2 `ZipfileBackend`：早点发现、分类正确

- `inspect()`（31 行）在遍历中央目录时，只要发现**非目录**成员的 `compress_type` 不在
  `{0, 8, 12, 14}` 内（或带 AES 标志），立即抛 `ArchiveBackendUnsupported`，消息里带成员名
  和方法号。清单阶段就能判断，不需要先解压、也不需要密码。
- `test_password` / `extract` / `stream_pages`：把 `NotImplementedError` 的 `except` 移到
  `RuntimeError` **之前**，并映射成 `ArchiveBackendUnsupported`（防止将来有人只改一处）。
  修好 2.1 之后，「密码不对」与「方法不支持」才真正是两种错误。

### 3.3 `ArchiveProcessor`：按顺序试后端，只在装包前回退

把现有的「inspect + 解析密码」抽成一个内部步骤（暂名 `_admit`），`process()` 改为：

```python
last_error = None
for profile in self.eligible_profiles(source_format):   # 顺序不变：先 zipfile，后 7zz
    backend = self.build_backend(profile)
    try:
        manifest, password_id, password = await-ish self._admit(backend, volumes)
    except ArchiveBackendUnsupported as exc:
        last_error = exc        # 这个后端读不了 → 换下一个
        continue
    break
else:
    raise last_error            # 都读不了：如实报「方法不支持」
snapshot = ...                  # 快照在选定后端之后构造，tool_profile 记录真正干活的那个
```

- **只在 `_admit` 阶段回退**：解压/打包阶段的失败不回退（listing 已经告诉我们方法是否支持；
  中途失败是另一类问题，重跑一遍等于把同一份工作做两遍，还可能留下半成品）。
- 顺序、快照字段、安全门禁、原子发布、密码尝试顺序（`last_success → priority → id`）都不变。
- 选中的后端若没有可用的下一个 profile（7-Zip 被禁用或没装），抛 `last_error`，页面
  说明「内置解压器不支持该 ZIP 的方法，请在『设置 → 归档』确认 7-Zip profile 已启用」，
  而不是「密码不对」。

## 4. 备选方案与取舍

- **备选 A：把 `zipfile` profile 排到 7-Zip 之后。** 一行改动，但会让所有普通 ZIP 也走全量
  解压，丢掉 512MB 档唯一的内存保证（内置是流式直写 CBZ）。否。
- **备选 B：只修 2.1（错误分类）。** 报错会变准确（「方法不支持」而不是「密码不对」），
  但 AES-256 ZIP 依然打不开，文档承诺的回退仍然缺失——运营者的问题没解决。否。
- **备选 C：解压阶段也回退。** 重复解压、可能留下临时文件，而且 listing 阶段已经有答案。否。
- **备选 D：用 Central Directory 预扫描决定 profile（不做异常驱动）。** 本质是同一件事，
  但需要把 zipfile 的领域知识搬到 `ArchiveProcessor` 里；放在 `ZipfileBackend` 内部更内聚。
  取本方案，把 D 的做法当作 3.2 的实现细节。

## 5. 影响面

- **行为**：AES-256 / Deflate64 / 其它非 stdlib 方法的 ZIP，从「待补密码」变为正常打包
  （走 7-Zip profile，任务详情里 `backend=seven_zip`）；普通 ZIP 仍走内置流式路径。
- **错误文案**：方法不支持时不再伪装成密码问题；7-Zip 不可用时给出可操作的提示。
- **兼容**：不动数据库、不加环境变量、不改页面结构、不换错误码词表（除新增内部类）。
- **RAR**：见 §9 附录。读路径（RAR3/RAR5、`-hp` 头部加密、固体、`.partN`/`.rNN` 分卷）本次已用
  上游真实夹具在 7zz 上实测通过，但夹具里没有图片，「RAR → CBZ 完整流水线」仍未端到端跑过；
  并入 §9.6 的夹具计划后收口。验证过程中另发现旧式 `.rNN` 分卷只从 `.rNN` 出发能被发现
  （§9.3），随本次一并修。

## 6. 测试计划

单元（不需要 7-Zip，能在任何机器上跑）：

1. 造一个方法号被改成 99 的 ZIP（写完真实 ZIP 后改 local header + central directory 的方法字节），
   断言 `ZipfileBackend.inspect()` 抛 `ArchiveBackendUnsupported`（而不是返回清单）。
2. 让 `ZipFile.open` 抛 `NotImplementedError`，断言 `test_password()` 抛的是
   `ArchiveBackendUnsupported`，**不是** `ArchivePasswordRequired`（锁死 2.1 的 except 顺序）。
3. `ArchiveProcessor`：第一个 profile 抛 `ArchiveBackendUnsupported` 时改用下一个（注入假后端），
   并断言快照记录的是第二个 profile；两个都不可用时错误码不是 `ARCHIVE_PASSWORD_REQUIRED`。
4. 密码尝试顺序回归：ZipCrypto ZIP、7z 加密包仍然按 `last_success → priority → id` 命中。

集成（有真 7-Zip 才跑，没有则 skip，沿用 `test_seven_zip_real.py` 的跳过方式）：

5. `-tzip -mem=AES256 -pS3cret` + 正确密码 → 出 CBZ、`password_id` 记录、
   `snapshot.backend == "seven_zip"`、页数正确。
6. `-tzip -mm=Deflate64 -pS3cret` 同上。
7. 无密码的 Deflate64 ZIP → 成功（当前是「待补密码」）。
8. 回归：ZipCrypto ZIP 仍 `snapshot.backend == "zipfile"`（流式路径没被牺牲）。
9. AES ZIP + 空密码库 → 回退到 7-Zip 后如实报 `ARCHIVE_PASSWORD_REQUIRED`（而不是方法错误）。
10. 回退后的压缩率门语义不变：一个全是空白页（压得掉几百倍）的 AES ZIP 仍能发布，
    证明 ZIP 在 7zz 下确实按成员而非按块分组。

预计 +8~10 条用例；基线在 R51 条目里更新（当前 1650）。

## 7. 文档同步

- `docs/USAGE.md` 归档处理一节（229 行那条）：把「`zipfile` 无法打开的加密 ZIP」写实——
  内置只能读 Stored / Deflate / Bzip2 / LZMA，AES-256、Deflate64 等**自动回退**到 7-Zip
  profile；并写清「密码不对」与「方法不支持」是两种错误、两种处置。
- `README.md`（如能力清单提到归档后端）与 `AgentHelp/EHBot.md` 的归档小节同步；
  若最终没有新的环境变量/页面项，按惯例说明「无表格型事实变更」。
- `AgentHelp/progress.md` 追加 R51 条目，`AgentHelp/PHASES.md` 加一行，
  `AgentHelp/AGENTS.md` 更新基线链。

## 8. 风险

- 回退会改变「7-Zip profile 被禁用」这类部署的报错文案（变得更准确，但确实是变化）。
- AES ZIP 走 7-Zip 后不再流式，需要临时解压目录——这条路径本来就有（`extract-*` + 原子发布），
  512MB 档的内存承诺只对「内置能读的 ZIP」继续成立，文档要写明这个边界。
- **已验证**：7-Zip 对 ZIP 的 `-slt` 给出每个成员自己的 `Packed Size`，且**没有 `Block` 字段**
  （对 7z 才有），所以回退后 R48 的压缩块比例门对 ZIP 仍然是「每个成员一组」，
  语义与内置路径一致；集成用例照旧覆盖高压缩比页面。

## 9. 附录：RAR 工具链评估（2026-10-01 追加，回应「加一个 RAR 工具链」）

### 9.0 结论

**不建议把 rarlab 的 `rar`/`unrar` 打进镜像；RAR 现在这条 7zz 路径已经能用，缺的是验证。**
运营者报告的「加密包打不开」是 ZIP 的 AES-256 加后端不回退（第 1、2 节），与 RAR 无关：
RAR 从一开始就走 `7zz-default`，从不经过内置 `zipfile`，所以它不会掉进 2.1 那个
「不认识的压缩方法被当成密码错误」的坑。

本次用仓库自带的 7-Zip 26.02 加**上游真实 RAR 夹具**做了实测（9.1）：RAR3 / RAR5、
头部加密 `-hp`、数据加密、固体、两种分卷都能读。反倒是在验证过程中发现了一个
**与 RAR 分卷有关的真实缺陷**（9.3）。所以本附录的建议是：

1. 照做第 3 节的 ZIP 回退——那才是运营者遇到的问题。
2. **不加托管 RAR 工具链**；把 RAR 从「未验证」升级为「已验证」：补图片 RAR 夹具与集成用例。
3. 顺带修 9.3 的分卷发现缺陷。
4. 若确实想要一条兜底读路径，唯一符合许可的选项是**可选、默认关闭、由运营者自备可执行文件**
   的 `unrar` CLI profile（9.4 的 B 方案）；收益很窄，成本见 9.5，可选。

### 9.1 实测证据（本机，7-Zip 26.02，夹具取自上游 `markokr/rarfile` 测试集）

夹具只用于本次本地验证、**未入库**；能否入库见 9.6 第 1、2 条。

| 夹具 | 类型 | 结果 |
|------|------|------|
| `rar5-hpsw.rar` | RAR5 头部加密（`-hp`），密码 `password` | 无密码报 `Cannot open encrypted archive. Wrong password?` → 命中 `_PASSWORD_MARKERS`，正确定位为「需要密码」；给密码后 `inspect` / `test` / `extract` 全部成功 |
| `rar3-comment-hpsw.rar` | RAR3 头部加密 | 同上 |
| `rar5-psw.rar` | RAR5 仅数据加密 | 无密码也能列清单，`Encrypted = +` → 正常走密码库 |
| `rar5-solid-qo.rar` | RAR5 固体 | 4 个成员各自有 `Packed Size`，**没有 `Block` 字段** |
| `rar3-vols.part1/2/3.rar` | RAR3 `.partN.rar` 分卷 | 从 part1 跨卷列出并解出全部成员；`resolve_volumes()` 顺序正确 |
| `rar3-old.rar/.r00/.r01` | RAR3 旧式 `.rNN` 分卷 | 从 `.r00`/`.r01` 出发能发现三卷；**从 `.rar` 出发发现不了**（见 9.3） |

两条对现有设计的确认：

- 第 2.1 的 except 顺序问题**不影响 RAR**：RAR 永远走 7zz，而 7zz 的「要密码」文案与
  `_PASSWORD_MARKERS` 完全对得上。
- R48 的比例门在 RAR 上仍是「按成员」：7zz 对 RAR（含固体）不给 `Block` 字段，所以
  `ArchiveMember.block` 保持 `None`，与内置 ZIP 路径语义一致。

### 9.2 为什么不打包 rarlab 的工具

- `rar`（能**创建** RAR 的那个）：EULA 是 40 天试用，并明写「未经书面许可不得放进其它软件包内
  分发」——把二进制塞进 EhBot 镜像正是它禁止的那件事。且 rarlab 没有 GitHub Release 这样的
  稳定发布 API 与校验和文件，`rarlinux-x64-<版本>.tar.gz` 每次改版都要人工重新对 digest；
  更要命的是**官方没有 linux-arm64 / arm 构建**（只有 x64 的 Linux/BSD/macOS），会直接砍掉
  现有 7-Zip 工具链支持的 arm64 平台。
- `unrar`（只解不压）：许可这一关反而是过的——rarlab EULA 明确把 UnRAR 组件排除在外，
  UnRAR 源码许可第 2、3 条写明「可自由分发、可放进其它软件包」。但它相比 7zz **几乎买不到
  额外能力**（见下表），所以「能打包」不等于「值得打包」。
- 差分能力表：

| 能力 | 7zz（已内置） | unrar | rar |
|------|----------------|-------|-----|
| 读 RAR3 / RAR5 | 是 | 是 | 是 |
| 读头部加密 `-hp`（含加密文件名） | 是（本次实测） | 是 | 是 |
| 固体 / `.partN.rar` / `.rNN` 分卷读 | 是（本次实测） | 是 | 是 |
| 按恢复记录修复损坏的分卷 | 否 | 否（unrar 只解不修） | 是 |
| 创建 RAR | 否 | 否 | 是 |

  真正只有 `rar` 能做的是「创建」与「用恢复记录修复损坏包」：前者 EhBot 用不到（产物是 CBZ），
  后者是运维动作而不是打包流水线的一环——而那恰恰是不能分发的那个二进制。

### 9.3 顺带发现：旧式 `.rNN` 分卷只在一个方向被发现（真实缺陷）

`app/archive/formats.py` 的 `volume_group()` 对 `name.partN.rar` 返回 `name.rar`，对 `name.rNN`
也返回 `name.rar`，但对**领头的 `name.rar` 本身返回 `None`**：

```
rar3-old.rar   group=None             vols=['rar3-old.rar']                               ← 漏掉 .r00/.r01
rar3-old.r00   group=rar3-old.rar     vols=['rar3-old.rar','rar3-old.r00','rar3-old.r01'] ← 正确
```

`_sibling_volumes()` 里 `if sibling.name == group` 那句只在「从 `.rNN` 出发」时才补上领头的
`.rar`，反方向没有对称处理。后果：当下载产物恰好是领头的 `.rar` 时，`resolve_volumes()` 只返回
一卷，`ARCHIVE_VOLUMES_MISSING` 这一关**形同虚设**（`indexes=[1]`，`expected` 也是 `{1}`）；
能不能解出来全靠 7zz 自己在同目录按命名找分卷——也就是安全门禁被绕过，缺卷时不会以「缺卷」报错。

修法很小：`volume_group()` 在 `.partN.rar` / `.rNN` 之后，对后缀为 `.rar` / `.cbr` 的路径也返回
`f"{path.stem}.rar"`。单个 `name.rar` 会因此多做一次同目录扫描，但 `_sibling_volumes` 只会选出
它自己，`resolve_volumes` 结果不变；`.rNN` 系列从此两个方向都能发现完整。要单独加用例（9.6 第 6 条）。

### 9.4 备选方案

- **A（推荐）：不加 RAR 工具链。** 只做第 3 节 + 9.3 + 补齐 RAR 验证（夹具与集成用例）。
  成本最低，覆盖运营者的真实问题；RAR 能力以 7zz 为准，文档写清「不支持修复损坏包」。
- **B（可选，代价见 9.5）：A + 一条「自备 `unrar`」兜底读路径。**
  - 新增 `BACKEND_UNRAR = "unrar"`；`UnrarBackend` 实现 `inspect` / `test_password` / `extract` /
    `pack_cbz`（打包复用内置 zipfile 的 `ZIP_STORED`——CBZ 本来就是 ZIP，不需要外部工具）。
  - 新增 profile `unrar-default`：`kind=CLI`、`formats=["rar"]`、
    `capabilities=["password","volumes","external_binary"]`、**`enabled=0`**；由运营者填绝对路径后
    手动启用。
  - 回退顺序（接 3.3 节）：`zipfile → 7zz → unrar`，且只在 `_admit` 阶段换后端。
  - 不下载、不分发任何 rarlab 二进制；Windows / macOS 同样只支持操作员自备。
- **C：托管 rarlab 工具链（含 `rar`）。不建议**，原因见 9.2；若运营者坚持，也要先取得
  win.rar GmbH 的书面许可，并接受 arm64 无官方资产。

### 9.5 B 方案的影响面（只有选 B 才需要动）

- `app/archive/models.py`：`BACKEND_UNRAR`。
- `app/archive/backends/unrar.py`：新后端。`unrar` 的机器可读输出用 `unrar lt`（技术列表）比
  `l` 稳定；要处理无密码时不进交互（`-p-`）。
- `app/archive/processor.py`：`build_backend()` 加分支；回退顺序里排在 7zz 之后。
- 迁移 `023_*.sql`（append-only，插一行默认禁用的 profile）。
- `app/api/serializers.py`、`app/web/templates/settings/_archive.html`：现有 CLI profile 已能改
  可执行路径，无需新控件，只需文案。
- 文档：`docs/USAGE.md`、`README.md`、`AgentHelp/EHBot.md`。

### 9.6 测试计划（A 方案）

1. 让「RAR 已验证」落地需要**带图片**的 RAR 夹具：加 `scripts/make_rar_fixtures.py`，用操作员
   自备的 `rar` 生成一次（RAR3 数据加密、RAR5 `-hp`、RAR5 固体、`.partN` 分卷、`.rNN` 旧式分卷），
   产物提交到 `tests/fixtures/rar/`；生成器在没有 `rar` 时自己跳过。夹具是数据不是软件，
   提交它们让 CI 不需要 `rar` 也能跑。
2. 提交的夹具覆盖：RAR5 `-hp` + 正确密码 → 出 CBZ、`password_id` 记录、
   `snapshot.backend == "seven_zip"`、页数正确。
3. RAR3 `.partN.rar` 分卷 → `volume_count == 3`、页数正确。
4. RAR5 固体 → 比例门按成员放行合法图片、拦住伪装成图片的垃圾。
5. 密码库只有正确密码时，头部加密 RAR 不再报 `ARCHIVE_PASSWORD_REQUIRED`。
6. 9.3 的对称性：`resolve_volumes(name.rar)` 与 `resolve_volumes(name.r00)` 返回同一组卷。
7. `scripts/verify_docker_linux.py` 里「RAR 支持」目前只查 `7zz i` 的格式表，改成用夹具真的
   `l` / `t` 一次。
8. 若选 B，另加：`ArchiveBackendUnsupported` 使 7zz 让位给 unrar（注入假后端）。

### 9.7 未验证项 / 风险

- 本次实测用的是上游夹具，**里面没有图片**，所以「RAR → CBZ 的完整图片流水线」仍未端到端跑过；
  要有 `rar` 生成的图片夹具才能闭环（9.6 第 1 条）。R51 条目的「未验证项」要照实写。
- `unrar` 输出格式随版本变化（5.x / 6.x / 7.x；Debian 的 `unrar-free` 只到 RAR3），B 方案要
  限定并声明支持的最小版本。
- 许可：`rar` 不随镜像分发；B 方案只调用操作员自备的 `unrar`，EhBot 仓库与镜像不含 rarlab 二进制。
- 7-Zip 不能用恢复记录修复损坏 RAR——这是能力边界而不是缺陷，文档要写明。

### 9.8 文档同步（接第 7 节）

- `docs/USAGE.md` 229 行：写清 RAR 由 7zz 读取（RAR3 / RAR5、`-hp`、固体、`.partN` / `.rNN` 分卷），
  读不了损坏包、不会用恢复记录修复；若选 B，补 `unrar` profile 的启用说明。
- `README.md` 与 `AgentHelp/EHBot.md` 的归档小节同步能力与边界。

## 10. 追加修复：跨挂载打包失败（2026-10-01，运营者实测）

运营者在测试镜像时贴回堆栈：`app/archive/backends/seven_zip.py` 的 `pack_cbz` 抛
`OSError: [Errno 18] Invalid cross-device link`。原因不是本次回退引入的，而是这条路径一直
用 `st_dev` 预判能否硬链接：工作目录与书库是不同挂载时整个作业失败；更隐蔽的是**同一宿主
文件系统的两处 bind mount**——`st_dev` 相同、`os.link` 仍返回 `EXDEV`，预判本身不可靠。

修法：改为「先试硬链接、失败即复制」（`_link_or_copy`），与 `app/torrent/delivery.py` 收货
时的写法一致——硬链接只是省一次复制的优化，不能当成正确性前提。它不属于第 3 节的回退方案，
但它让**任何**走 7-Zip 后端的包（RAR、7Z、以及第 3 节新纳入的 AES / Deflate64 ZIP）都能在
工作目录与书库不同挂载时打包成功，因此记在本阶段。
