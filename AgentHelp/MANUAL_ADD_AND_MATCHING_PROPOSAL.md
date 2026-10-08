# 方案：手动添加判重 + 自动决策的匹配详情 + 预览链接里的画廊 ID（R60，2026-10-08）

**状态：待审阅。** 本阶段把运营者 2026-10-08 的三条指示合成一个交付：
① 手动添加（画廊 **与磁链**）判重；② 自动审批规则加「匹配详情」；③ 候选准入解析预览链接里的画廊 ID。
经运营者确认：合集帖**不拆分**——「既带预览链接又带 Eh 链接的消息能正常解析即可」。

数据来源：运营者放进仓库的参考消息
`AgentHelp/reference_messages/ChatExport_2026-10-08/`（已移入忽略目录，见 §2.4）。

---

## 1. 需求

| # | 需求 | 运营者原话 / 来源 |
|---|------|------------------|
| R60-1 | 手动添加判重：画廊 ID 单列判重；**磁力链接也要判重**；已存在时**提示**（不建行、不 500、给跳转） | 「磁链加上重复检测，已存在的进行提示」 |
| R60-2 | 自动审批规则命中时给出**匹配详情**：命中了哪一条条件、当时读到的什么值 | 「规则匹配加上匹配详情」 |
| R60-3 | 候选准入解析预览链接里的画廊 ID：`https://telegra.ph/ehentai-4137423-9ce70ddc26-10-06` → 画廊 4137423 / token 9ce70ddc26 | 「大部分都有 Eh 链接，有些没有，但是预览连接就已经体现了 Eh 链接……如果不方便进行解析，可以考虑丢给 AI」 |
| — | 参考消息归档：放 `AgentHelp/` 下、进忽略目录、不上传远程 | 「将参考的消息文档加入忽略文件夹，不上传到远程仓库，放在 AgentHelp 文件夹」 |

---

## 2. 现状与证据

### 2.1 手动添加会 500（栈与复现）

线上栈（`POST /manual-add`）：

```
app/web/routes/manual_add.py:69  _ingest_manual_eh
  -> app/db/database.py:2904     create_manual_candidate
  -> app/db/database.py:2924     _create_manual_candidate_sync 的裸 INSERT
sqlite3.IntegrityError: UNIQUE constraint failed: candidates.ex_gid, candidates.ex_gallery_token
```

未捕获 -> 500。**已本地复现**（同一链接连贴两次，第二次逐字相同的 `IntegrityError`）。

根因：R59 把「一个画廊一件候选」定为硬规则，但只在**摄取管线**上落了门（`CandidateIngestor._gate` +
`_save_candidate_message_sync` 的并发裁决）。「手动添加」是第三条入池路径：它不经过 ingestor，
`create_manual_candidate` 直接以 `APPROVED` 插入 `candidates`，于是撞上 001 迁移就存在的唯一索引
`UNIQUE (ex_gid, ex_gallery_token)`。

同一个不变量的另一半也还开着：唯一索引是**两列**，而 R59 的判据是**画廊 ID 单列**——所以「同 ID、不同
token」的手动添加今天不会报错，但会静悄悄建出第二条候选。磁力链接则完全没有判重：同一个 `btih` 贴两次
就建两条候选、排两次下载。

### 2.2 自动审批规则：为什么需要「匹配详情」

**本轮做过的事**：把运营者那条规则用**整条真实链路**跑了一遍——网页表单提交 -> `parse_rule_condition`
-> `validate_rule_ast` -> 落库 -> `AutoApprovalSweeper.sweep_once`。存下来的 `dsl_snapshot` 与运营者贴的
一字不差，`action=REJECT`、`group_operator=OR`；对一个「Tags 里没有 NTR/cosplay/AI/3D、Category=Manga」
的候选，sweep 结果 `decided: 0`，状态仍是 `PENDING_REVIEW`。

**结论**：这条规则形状下，引擎没有出现「不命中却驳回」。所以真正要交付的不是改判定，而是**让判定可解释**
——运营者现在只能看到结果（「已驳回」+「命中规则「X」」），看不到**命中了哪一条条件、当时读到的值**，
于是命中看起来永远像随机的。

而且「已驳回」不止一条来源。把候选置为 `REJECTED` 的路径**只有三条**，理由文案各不相同：

| 来源 | 触发 | `filter_reason` 形状 | 能看到的地方 |
|------|------|---------------------|--------------|
| 自动规则命中 | 规则 `action=REJECT` 且条件命中（R58） | `命中规则「名称」` | 时间线 `AUTO_REJECT` + 详情页「筛选结果」 |
| 人工 / 批量驳回 | 运营者按的按钮 | 运营者填的备注（可空） | 时间线 `REJECT` |
| **来源元数据规则** | 补齐元数据时 `re_evaluate_candidate_metadata_rules` 判定 `IGNORE`（`app/candidates/rules.py` 的 `evaluate_metadata_rules`：`required_tags` / `forbidden_tags` / `allowed_languages` / `allowed_categories` / `min_rating`） | `缺少必需 Tag X` / `包含禁用 Tag X` / `语言 x 不在允许列表` / …… | 只有详情页「筛选结果」那行 note，**时间线上没有任何痕迹** |

第三条是这次的重点发现：它是**在补齐元数据时自动发生**的（页面渲染和 sweeper 的 `_enrich_metadata` 都会
触发），状态直接变成「已驳回」，而时间线里查不到任何记录——运营者完全可能把它看成「自动驳回规则乱命中」。
「匹配详情」因此要顺带把这三条来源区分开（见 §4）。

审计快照里其实**已经存了**解释所需的一切（`_record_automatic_decision` 写入
`review_actions.details_json`）：`rule_id` / `rule_name` / `rule_version` / `dsl_snapshot` / `condition` /
`conditions`（每条条件的 dsl 与 `matched`）/ `metadata`（判定时读到的字段值）。只是**没有任何界面把它读出来**。

### 2.3 参考消息的统计（准入要解析什么）

对参考导出（267 条消息）逐条统计：

- 267 条消息，其中 **219 条**带 ExHentai/e-hentai 画廊链接；
- **15 条**只有 `telegra.ph/ehentai-<gid>-<token>-MM-DD` 形状的预览链接——画廊 ID 与 token 就在
  slug 里，**可以正则解析，不必丢给 AI**（例：`ehentai-4137423-9ce70ddc26-10-06` -> 4137423 /
  9ce70ddc26；另见 `-11-19` 结尾与 `C108-…`、`diletta-…` 这类**真人标题**的 slug，后者不能认）；
- **57 条是合集帖**：一条消息里 2～21 部作品（每部一段「预览：<telegra.ph>」+「原始地址：<EH URL>」），
  合计 493 部作品。今天一条消息只取**第一个**画廊 ID -> 一条候选，所以最多 274 部作品根本进不了候选。

按运营者答复，**合集帖本轮不拆分**（见 §6）。

### 2.4 参考消息归档（已完成）

- `tests/ChatExport_2026-10-08/` -> `AgentHelp/reference_messages/ChatExport_2026-10-08/`；
- `.gitignore` 加 `/AgentHelp/reference_messages/`，整份导出不入库、不上远程；
- 因为不入库，**测试不能依赖它**：需要的样本摘录成 `tests/fixtures/reference_messages.json`
  （只留原消息的 `text` / `entities` / `message_id` 等最小字段，十几条，见 §7）。

---

## 3. 设计 A：手动添加判重（画廊 ID + 磁链）

**判据（都是单列，与 R59 口径一致）**

- 画廊：`ex_gid` 单列（token 只是 URL 的拼法，不是作品身份）；
- 磁链：`torrent_hash` 单列（小写比较；tracker 与显示名不是身份）。

**位置：一次事务内完成**（`app/db/database.py`）

新增异常 `DuplicateCandidateError(ValueError)` 与两个子类 `DuplicateGalleryCandidate` /
`DuplicateTorrentCandidate`（`label` 分别是「画廊」「磁力链接」），携带 `existing_candidate_id`。
`_create_manual_candidate_sync` 在同一个连接里：

1. 先查（`_manual_duplicate_sync`）：按 ID 单列查画廊、按小写 infohash 查磁链，命中即抛出对应异常；
2. `INSERT` 外面补 `try/except sqlite3.IntegrityError` 作为**并发裁决**（与 R59 的
   `save_candidate_message` 同角色）：捕获后按画廊 ID 再查一次，查到即抛 `DuplicateGalleryCandidate`，
   查不到**原样重抛**——真正的约束故障绝不能被读成「重复」。

放在 DB 层而不是只放路由层，是因为「先查后插」在路由层不构成仲裁：写闸门必须与写语句在同一个事务里。
元数据 Title 的写入本就在同一事务内，异常抛出即整体回滚，不会留下半条候选。

**取舍（写在代码注释里）**：磁链列没有唯一索引（现有部署里可能已经有重复行，加唯一索引的迁移会在这种库上
直接失败），所以磁链的并发窗口由「同一事务内先查」覆盖，单运营者表单下足够；画廊那一路由唯一索引兜底。

**路由与页面**（`app/web/routes/manual_add.py`、`manual_add.html`）

- 捕获 `DuplicateCandidateError`，渲染 `/manual-add`：
  `error` =「该<label>已有候选 #N，本次未重复添加。」，并新增 `existing_candidate_id` 传给模板，
  在提示下方渲染「查看已有候选 #N」按钮（指向 `/works/N`）；
- **停在表单，不自动跳转**（运营者答复 2）：提示是提示，跳转由运营者点那个按钮决定；
- HTTP **409 Conflict**（表单其他失败仍是 400：那些是链接本身不合法）；
- 不抓元数据、不入队、不建行、不写第二条 download job。

---

## 4. 设计 B：自动决策的匹配详情

**不改任何决策路径**（判定、落库、审计一字不动），只把**已经存下来的解释**读出来给人看。

1. **作品详情页时间线**：`AUTO_APPROVE` / `AUTO_REJECT` 条目下新增一块「匹配详情」：
   - 规则名 + 版本 + `dsl_snapshot`（当时那一版）；
   - 逐条列出 `conditions[]`：`dsl` + 命中与否（命中的在前且高亮，未命中的折叠在 `<details>` 里）；
   - 「判定时读到的值」：从快照的 `metadata` 里取**规则用到的字段**（`condition` 里出现过的 field，
     含 `TAG` -> `Tags`/`TagsRaw`），一格「字段 = 值」，值过长截断并提供 title；
   - 一句话结论：「命中条件：{Category} LIKE "%cosplay%"（Category = "Cosplay"）」，无条件命中则说明
     「本条由…」（理论上不会出现，作为兜底）。
2. **候选页与已下载页的列表行**（运营者答复 1：两处都要）：加一行小字筛选原因——现在列表**完全没有**这行，
   只有详情页的「筛选结果」note 有。文案直接用 `filter_reason`，不做二次渲染。
   **只在它解释得通的时候显示**：状态是已驳回/待补充，或 `filter_result` 不是 `ACCEPT`。
   正常通过的行（`ACCEPT`，例如「包含 ExHentai 画廊链接」）不显示——那是每行都有的噪声，
   不是「为什么」。已下载页同理：绝大多数行是正常完成，因此多数时候那一行不出现。
3. **三条来源区分开**：详情页「筛选结果」那一行前面加来源标签，取值来自已有事实：
   - 时间线里最近一条 `AUTO_REJECT` -> 「自动规则」；
   - 有 `REJECT` 人工动作 -> 「人工」；
   - 都没有而状态是「已驳回」且 `filter_result=IGNORE` -> 「来源元数据规则」（并把该来源规则的名字/条件
     列出来——它存在 `telegram_sources.rules_json` 里，是本候选所属来源的那一份）。
     这样「为什么被驳回」在任何一条路径下都能一眼答出来。
4. **JSON API**：`app/api/serializers.py` 的 `review_action()` 增加 `conditions` / `metadata` 两个键
   （mobile 客户端同享），页面与 API 共用一份序列化，不各写一遍。
5. **纯读**：所有新增都是模板 + 序列化器 + 查询；没有新的写路径，也不改 `matching_rule`。

---

## 5. 设计 C：准入解析预览链接里的画廊 ID

**解析**（`app/candidates/links.py`）

```python
# /ehentai-4137423-9ce70ddc26-10-06、/ehentai-4181134-55431c0ecd-09-12-2
PREVIEW_GALLERY_PATTERN = re.compile(
    r"^/ehentai-(\d+)-([A-Za-z0-9]{4,})-(\d{2})-(\d{2})(?:-\d+)?$", re.IGNORECASE
)

def find_preview_gallery_ref(urls) -> tuple[int, str] | None:
    ...
```

- 只认**已知预览站**（`PREVIEW_HOSTS`：`telegra.ph` / `graph.org`）**路径**上的这一种 slug；
- `find_gallery_ref` **保持严格**（只认 Eh 的 `/g/<gid>/<token>`），新函数单独存在，名字就说明来源；
- 两个解析器（`app/candidates/ingestor.py`、`app/candidates/mtproto.py`）改成
  `gallery_ref = links.find_gallery_ref(urls, text) or links.find_preview_gallery_ref(page_urls)`，
  `filter_reason` 区分两种来源：「包含 ExHentai 画廊链接」/「由预览页链接识别 ExHentai 画廊」。

**效果**：参考数据里那 15 条从「无 ID 候选」变成正常候选——因而自动参与 R59 的去重闸门、也不再受
「无 ID 候选不被自动规则处理」的限制；配套的预览页链接照旧存进 `preview_urls`，下载来源链不变。

**兜底（错认的代价可控）**：slug 里的 `gid/token` 本来就是 EH 画廊地址的那两段，真错了也只会在补齐元数据
时拉不到数据（`gdata` 查无此画廊 -> 候选停在无元数据，人工可改链接），不会把别部作品的元数据搬过来。

**手动添加**：接受同样的形状（`find_gallery_ref(x) or find_preview_gallery_ref(x)`），页面的链接说明补一句
「也支持 `telegra.ph/ehentai-<gid>-<token>` 预览链接」。这是「粘贴预览链接」这个已存在动作的自然延伸。
（运营者答复 3：**允许**。）

**AI 不参与**：这条解析是纯字符串规则，稳定且可测，不需要模型判定（AI 候选判定是既有功能，本轮不动）。

**与既有设计记录的关系**：`TELEGRAPH_PREVIEW_PROPOSAL.md` 把频道消息分成「预览 + 原始地址（TELEGRAPH、
EXHENTAI）」与「纯预览（TELEGRAPH）」两种来源。本轮不动那张表——「纯预览」仍然是预览来源；只是其中
slug 形如 `ehentai-<gid>-<token>` 的那些**多了一个可识别的画廊 ID**，于是它们不再落入 R59「无 ID 候选」
的限制（能被去重闸门与自动规则处理）。反过来，没有这个 slug 的纯预览消息与今天完全一样。

---

## 6. 明确不做（本轮）

1. **合集帖不拆分**（运营者答复）。现状代价已在 §2.3 写明：一条合集消息只覆盖第一部作品。若以后要拆分，
   另开阶段——它要动 `candidate_messages` 的多对多写入、编辑消息的退订路径（`deactivate_candidate_message`
   现在按 `source_message_id` 删链路）与媒体组合并逻辑，不是「顺手改一下」。
2. **不用 AI 兜底解析**（见 §5 末）。
3. 不引入新的画廊去重策略、不改 R59 的闸门语义。

---

## 7. 测试计划（预计 +12 ~ +15）

`tests/integration/test_manual_add.py`（+5）
- 连贴两次同一画廊：第二次 409、页面出现「该画廊已有候选」与 `/works/N` 链接、`candidates` 仍只有一行；
- 同 ID、**不同 token** 也被拦（证明判据是 ID 单列）；
- 磁链贴两次：第二次 409、「该磁力链接已有候选」、`candidates` 仍只有一行、`download_jobs` 没有第二条；
- 重复尝试不改动已有行（状态 / `filter_reason` 不变）；
- 第一条候选被彻底删除后，同一个画廊 ID / infohash 可以重新添加（门读的是表，不是缓存）。

`tests/integration/test_database.py`（+2）
- `create_manual_candidate` 重复 ID 抛 `DuplicateGalleryCandidate` 且带 `existing_candidate_id`；
- 重复 infohash 抛 `DuplicateTorrentCandidate`（大小写不同的 btih 也算同一个）。

`tests/unit/test_links.py`（新增或扩写，+5）
- 运营者的例子 `ehentai-4137423-9ce70ddc26-10-06` 解析成 `(4137423, "9ce70ddc26")`；
- `-11-19` 结尾、`-2` 追加后缀两种形状；
- 反例：`C108-…`、`diletta-…` 这类真人标题的不匹配；`ehentai-abc-…`（ID 非数字）不匹配；
- 非预览站 host 不匹配；`find_gallery_ref` 对预览链接仍然返回 None（严格性没被放宽）。

`tests/integration/test_candidate_ingestion.py`（+2）
- 只有 `ehentai-<gid>-<token>` 预览链接的消息：解析出画廊 ID、`filter_reason` 说明来源、能参与去重闸门
  （同画廊的第二条被忽略）；
- 既带预览链接又带 Eh 链接的消息：画廊 ID 取 Eh 链接（严格优先），预览页照旧留存。

`tests/integration/test_work_detail_web.py` / `test_candidates_web.py`（+3）
- AUTO_REJECT 条目渲染出命中的那条条件与判定时读到的值；
- 列表行显示筛选原因；
- 由来源元数据规则驳回（`filter_result=IGNORE`、无 `AUTO_REJECT`）时显示「来源元数据规则」而不是「自动规则」。

样本：`tests/fixtures/reference_messages.json`（从参考导出摘录的最小字段，入库；参考导出本身不入库）。
运营者答复 4：**允许**摘录里保留作品标题原文（成人向标题照留）。

---

## 8. 文档同步清单

- `docs/USAGE.md`：手动添加（重复链接会被指出 + 磁链也判重 + 支持预览链接）；自动审批（时间线上的匹配
  详情）；候选准入（预览链接里的画廊 ID；合集帖一条消息只取第一部作品，明确写出来）；
- `README.md`：「主要能力」里「一个画廊一件候选」那条补上「手动添加同受此规则约束，磁力链接按 infohash 判重」；
- `AgentHelp/EHBot.md`：§4.4 审核流程（匹配详情）、§4.7 候选准入表（预览链接来源、手动添加也在门内）；
- `AgentHelp/progress.md` R60 条目 + `PHASES.md` 一行与基线链 + `AGENTS.md` 基线链、版本与迁移编号；
- `.gitignore` 的 `/AgentHelp/reference_messages/` 已在 §2.4 落地。

## 9. 运营者已定的四项（2026-10-08）

1. 列表行的筛选原因：**候选页与已下载页两处都要**（§4.2）。
2. 重复添加：**先提示，停在表单**，跳转由运营者点「查看已有候选 #N」决定（不自动跳，§3）。
3. 手动添加**允许**粘贴 `telegra.ph/ehentai-<gid>-<token>` 预览链接（§5）。
4. `tests/fixtures/reference_messages.json` **允许**保留作品标题原文（§7）。

**开工前只剩一件事**：运营者对本方案的 go-ahead。开工后按仓库规矩一次提交落地
（实现 + 测试 + `progress.md` / `PHASES.md` / `AGENTS.md` / `README.md` / `docs/USAGE.md` 同步），
版本号不动（仍是 `v0.3.0rc3`），无数据库迁移。
