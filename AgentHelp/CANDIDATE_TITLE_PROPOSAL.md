# 方案 v6：标题只来自画廊元数据（无标题的候选进「待补充」）

**状态：待审阅（2026-10-11，v6）。等运营者 go-ahead 再动手。** 代码未改动。
v6 把运营者对 v5 的三点纠正写进来：元数据在**加入候选后立刻解析**；解析不了用 **ID 占位**；
「相邻消息配对」不再考虑（单图/单附件本身不会成为候选）。手动添加的改造在
`AgentHelp/MANUAL_ADD_FORM_PROPOSAL.md`。

关联：`AgentHelp/AGENTS.md`（先方案后开工）、`CANDIDATE_ADMISSION_AND_DELETE_PROPOSAL.md`（R50，
待补充）、`CANDIDATE_DEDUPLICATION_PROPOSAL.md`（R59，画廊 ID 闸门）、`SEARCH_FILTER_PROPOSAL.md`（R61）。
阶段号：落地后为 **R62**；**不需要数据库迁移**。

---

## 0. 需求与答复

### 0.1 原始问题

> 现在所有候选卡片的标题要么是候选编号，要么是部分分类，一点都不好进行分辨。
> 应该改为使用显示作品名称。

### 0.2 已定结论（v2–v6 累积）

| # | 结论 | 落到 |
|---|---|---|
| 1 | 作品名**优先日文名**，没有才英文名 | §2.1 |
| 2 | 网页卡片**跟随「标题来源」设置** | §2.1 |
| 3 | 归档名 / 规则输入也**画廊优先** | §2.2 |
| 4 | 卡片副行加 `#<画廊ID>` | §3 |
| 5 | 历史数据**启动时维护一次**，用一个字段记录 | §4 |
| 6 | 人工维护过的行**有标识、跳过**，其余都处理 | §4.2 |
| 7 | 标题与标签无关（Tag 的显示也不改） | — |
| 8 | **不再从 TG 消息解析标题**；拿不到画廊标题 → 待补充，人工处理 | §2.3 |
| 9 | **元数据在加入候选后就解析**；解析不了用 **ID 占位** | §2.3 / §2.4 |
| 10 | 「相邻消息配对」不用管：单图/单附件本来就不会成为候选 | §2.5 |

---

## 1. 现状与成因

**卡片标题就是一条 SQL 直查库里**（`metadata_values`，无缓存无派生表），六处共用同一段子查询
（`app/db/database.py:183`、`:257`、`:1738`、`:2469`、`:2623`、`:2836`、`:3312`），
按 `is_manual DESC, confidence DESC` 取一行。

**坏数据怎么来的**：消息解析器把 caption 的第一条非 URL 行当标题，写成 `TELEGRAM`、
`confidence = 0.9`；抓回来的画廊标题只有 `0.6`；取值先比 confidence，于是画廊名永远选不上。
同样的启发式抄了三份（`app/candidates/ingestor.py`、`app/candidates/mtproto.py`、
`app/db/database.py` 的编辑/删除重算）。

**「待补充」的设施本来就有**（R50）：来源规则在 `message.title is None` 时给 `NEEDS_INFO`
（`app/candidates/rules.py:36`）；`_update_candidate_filter_state()`（`app/db/database.py:765`）
在没有 `Title` 行时保持待补充；`re_evaluate_candidate_metadata_rules()`（`:3830`）在元数据规则通过时
把它转回待审核。v6 只是把「消息解析器总会塞一个标题」这件事拿掉。

**元数据今天什么时候抓**（这是 v5 的漏洞，v6 补上）：只有三处——打开候选列表页
（`app/web/routes/candidates.py:187`）、自动审批 sweeper 的一批
（`app/wiring.py:700`，只含 `PENDING_REVIEW`）、打包前（`app/wiring.py:589`）。
**没有「加入候选后立即抓」这条路**，所以按新规则候选会卡在待补充直到有人打开页面。

---

## 2. 设计

### 2.1 显示 / 归档怎么取名字

唯一真源 `title_subquery()`，供六处共用：

```sql
ORDER BY mv.is_manual DESC,                        -- 人工永远第一
         CASE mv.field_name WHEN 'JapaneseTitle'   -- 语言层：跟随「标题来源」设置
              THEN <japanese?> ELSE 1 - <japanese?> END DESC,
         <来源优先级> DESC, mv.confidence DESC, mv.created_at DESC
```

- **语言层跟随「标题来源」**（`archive_settings.library_title_source`，默认 `japanese`）——
  在 SQL 里直接读那行设置，不把参数串遍十来个调用点；设置一改，网页卡片与库文件名同时变。
- **来源层级**：`EXHENTAI > OPERATOR > MANUAL_ADD > EH_TORRENT / TELEGRAPH > FILENAME >
  TELEGRAM > INFERRED`；后三个只对历史行有意义（§4 会删掉），留着是为了维护跑完前界面不乱。

### 2.2 归档命名 / 规则输入

- `Database._effective_metadata_sync()`（`:3270`）的来源顺序改成画廊优先（它同时喂**归档路径规则**
  与**自动审批规则**）；
- `ConversionService._fetch_metadata_sync()`（`app/conversion/service.py:1383`）今天**没有 ORDER BY**，
  而 `_metadata_lookup` 取第一行 → 打包用的名字取决于 SQLite 行走顺序，补上与上面一致的排序。

### 2.3 摄取不再解析标题，且加入后就抓元数据

- 三处解析器（ingestor / mtproto / 重算）**不再写 `Title` 行**，`TELEGRAM`/`FILENAME`/`INFERRED`
  三个来源从此不产生；`ParsedSourceMessage.title/title_source/title_confidence` 与解析规则里的
  「必须提供标题」开关（`title_required`，默认关——新规则下它没有可决定的东西）一并删除；
- **新增元数据补抓**：候选建立后立刻把它的 id 交给一个抓取队列，worker 用现有
  `ExHentaiService.enrich_missing_metadata()` 抓一次（服务不存在/未配置连接时安静跳过）；
  抓取永远不改状态，只写元数据；
- 状态仍旧只有一条规则：**有 `Title`/`JapaneseTitle` 行 → 待审核；没有 → 待补充**
  （摄取时由 `evaluate_source_rules` 的现有分支判定，抓完由新增的「标题门」重算）。
  两处判定共用一个 helper（`_candidate_title_missing()`），不再是两份判断。

抓取队列的形态见 §5-1（我建议一个独立的小 worker：ingest 只入队、不做网络请求）。

### 2.4 占位标题（显示层，不入库）

没有标题时**不写占位行**（库里保持"没有标题"这一事实），只在渲染时兜底：

| 情况 | 卡片显示 |
|---|---|
| 有 `Title`/`JapaneseTitle` | 作品名（日文优先） |
| 没有标题，有画廊 ID | `#<画廊ID>` |
| 没有标题，也没有画廊 ID | `候选 #<候选ID>` |

这样卡片不会空白、也还能互相区分，而状态照旧是待补充等人补标题。

### 2.5 「相邻消息配对」不动

`app/db/database.py:1729` 那段用「标题 casefold 相等」把 photo 和压缩包合成一件候选的启发式，
按运营者说明不再考虑：默认解析规则要求消息**带画廊链接**（`require_gallery_link = True`，
`accept_photo`/`accept_archive` 默认关），单图/单附件本身进不了候选；同一作品的多条消息由 R59 的
**画廊 ID 闸门**合并。实现上只需把那一段的标题相等条件去掉（`message.title` 字段会消失），
保留 media group / 回复同一条 / 同画廊 ID 三条判据。

---

## 3. 界面

- 卡片/行/详情标题位置不动，只换"取哪一个值"+ 加 §2.4 的兜底。
- 副行 `candidate_meta` 末尾加 `#<画廊ID>`（无 ID 不显示）。
- **待补充 tab 文案**（`app/web/routes/candidates.py:74`「缺标题、缺附件或需要修订的候选」）
  改成「等待画廊标题或抓取失败的候选，可人工补充标题」，空态提示同步改。

---

## 4. 历史数据维护（启动一次性）

### 4.1 跑在哪、怎么记

`app/wiring.py:296` 的 `build_lifespan` 里 `await database.initialize()` 之后跑一次；异常只记日志 +
`startup_errors`，不阻断启动。幂等标记写既有 kv 表 `system_settings` 的键 `title_maintenance_v1`
（值 = 完成时间 + 统计），**不加列、不加迁移**。

### 4.2 维护做什么

| 行 | 动作 |
|---|---|
| `is_manual = 1`（人工编辑）或 `is_locked = 1`（锁定） | **不动**（运营者的标识） |
| `value_source IN ('TELEGRAM','FILENAME','INFERRED')` 的 `Title` 行 | **删除** |
| `EXHENTAI` 的 `Title` / `JapaneseTitle` | 保留 |
| 删完后没有标题的候选 | 状态重算 → 待补充，等抓取或人工补（触发一次补抓） |

日志 `title_maintenance_completed`，`extra={"scanned":…, "deleted":…, "needs_info":…}`。

---

## 5. 待确认

| # | 问题 | 我的建议 |
|---|------|---------|
| 1 | 「加入候选后立刻抓元数据」怎么实现：**独立抓取 worker**（ingest 只入队）还是**复用自动审批 sweeper**（每轮先补一批缺元数据的候选，再判定待审核的）？ | **独立 worker**（ingest 循环不做网络请求，失败可重试；sweeper 保持只做自动判定） |
| 2 | 占位规则是否就是 §2.4 那张表（有画廊 ID 用 `#<画廊ID>`，否则 `候选 #<候选ID>`）？ | 是；一句话就能改 |
| 3 | 删除 `ParsedSourceMessage.title/title_source/title_confidence` 与解析规则里的 `title_required` 开关？ | 删（新规则下没有生产者、也没有可决定的东西） |

---

## 6. 迁移 / 数据

- **不加迁移**：`metadata_values` 结构不变，`schema_migrations` 停在 `024`；维护标记用既有
  `system_settings`。版本号不变（`v0.3.0rc3`）。

---

## 7. 测试（要点）

- `tests/unit/`：`title_subquery()` 排序（人工第一、日文优先、跟随设置、画廊压过 Tg）；
  `_candidate_title_missing()` 判定。
- `tests/integration/test_candidate_ingestion.py`：带画廊链接的消息 → 库里**没有 `Title` 行**、
  状态待补充；人工编辑标题后 → 待审核。
- `tests/integration/test_exhentai_torrents.py`：抓取写入标题后候选从待补充转待审核；
  抓取成功但没有标题 → 仍待补充。
- `tests/integration/test_database.py`：六处读标题的位置对同一份数据给同一个答案；
  `effective_metadata()` 画廊优先。
- `tests/integration/test_title_maintenance.py`（新）：只删三类非人工行、人工/锁定行不动、
  删完落回待补充、标记存在时跳过、连跑两次零改动。
- `tests/integration/test_candidates_web.py` / `test_downloaded_web.py`：卡片显示画廊名，
  无标题时显示 §2.4 的占位，副行带 `#<gid>`。

---

## 8. 文档同步清单

- `README.md`：主要能力补「标题来自画廊元数据；取不到就进待补充」；
- `docs/USAGE.md`：候选状态流转（待补充 ↔ 待审核）、标题语言跟随「标题来源」、启动维护一句；
- `AgentHelp/EHBot.md`：§4 相关小节改写；
- `AgentHelp/progress.md` 追加 R62 条目；`PHASES.md` 一行 + 基线链；`AGENTS.md` 基线数字。

---

## 9. 落地顺序（go-ahead 后）

1. `title_subquery()` + 六处替换 + `_effective_metadata_sync` / `_fetch_metadata_sync` 排序；
2. 摄取三处不再写标题 + 删字段/开关；
3. 标题门 + 状态重算 + 抓取 worker（§5-1 的结论）；
4. 启动维护（§4）；
5. 占位显示 + `#<gid>` + 待补充文案（§3、§2.4）；
6. 测试 → 全量 → 文档同步 → **仅 `git add` 暂存，不推送**。
