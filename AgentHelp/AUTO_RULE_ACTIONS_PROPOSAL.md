# 方案：自动审批规则增加「自动驳回」动作（共用同一规则池）

> 状态：**已按本方案落地（R58，2026-10-06）**。实现记录见 `progress.md` 的 R58 条目，
> 阶段索引见 `PHASES.md`。
> v2 依据运营者 2026-10-06 的四点答复修订：命名用「自动通过 / 自动驳回」；把命中的规则名写进
> 被驳回候选的 `filter_reason`；**不加**保存二次确认；扫描器返回值/日志按 `approved/rejected`
> 分开计数。§9 的四条待确认即这四点。
> 关联：现有实现见 `AUTO_APPROVAL_PROPOSAL.md`、`progress.md`（R49/R50 前后的
> 自动审批条目）、`AgentHelp/AGENTS.md`。

## 0. 需求

运营者：「自动审批加一个自动驳回的选项，即将规则的操作进行分类，分为审批通过还是
自动驳回类型。共用同一个规则池，通过相同的优先级进行处理。」

拆成两条可验收的行为：

1. 每条自动审批规则带一个 **动作**：`通过` 或 `驳回`。
2. 两种动作的规则 **存在同一张表、同一个列表、同一条优先级队列**：
   按 `priority` 从小到大（同优先级按 id），取 **第一条命中且启用** 的规则，
   由它的动作决定候选的去向。不存在两套规则池、两个扫描器或两条优先级。

## 1. 现状

- 表 `auto_approval_rules`（`008` + `016` 迁移）字段：
  `id / name / enabled / priority / version / condition_json / dsl_snapshot /
  created_at / updated_at / case_sensitive`。没有「动作」概念。
- `AutomaticApprovalService.matching_rule(candidate_id)`
  （`app/auto_approval/service.py`）只接受 `PENDING_REVIEW` 候选，
  用 `effective_metadata` 顺序求值 `list_auto_approval_rules(enabled_only=True)`
  （SQL `ORDER BY priority, id`），返回第一条命中的规则。
- `ReviewOrchestrator.apply_automatic_approval(candidate_id)`
  （`app/review/orchestration.py`）拿到命中后走 `approve_and_enqueue`：
  置 `APPROVED`、按来源链入下载队列，再补一条 `AUTO_APPROVE` 审计。
- 两个触发点（都不改）：
  - `AutoApprovalSweeper.sweep_once()`：后台按 `auto_approval_interval_minutes`
    扫描待审核队列（无人在线也执行）；
  - `app/web/routes/candidates.py` 渲染待审核页时对当前页逐行调用，只是
    「顺手提前决策」的延迟优化。
- 人工驳回已存在：`ReviewOrchestrator.reject(ids, operator)`
  → `ReviewService.reject_candidate` → 置 `REJECTED`，写一条 `REJECT` 审计。
  `REJECTED` 在 `REQUEUEABLE_STATUSES` 里，可以从列表「重新排队」救回来。

结论：只需给「已被唯一命中的那条规则」再加一个动作，并在执行端把动作分派到
既有的 `approve_and_enqueue` 或 `reject`，规则池与优先级算法一行都不用改。

## 2. 设计：`auto_approval_rules.action`

### 2.1 取值

新增列 `action TEXT NOT NULL DEFAULT 'APPROVE' CHECK (action IN ('APPROVE','REJECT'))`。

- 默认 `APPROVE`：所有既有规则、以及任何不传动作的调用方，行为与今天完全一致
  （这是可回滚的关键——老数据库不需要人工干预）。
- 动作常量与 `app.review.models` 的审核动词**同值共用**，不另造两套拼写：
  规则里存 `APPROVE`/`REJECT`，执行后写的审计动作分别是
  `AUTO_APPROVE`（已有）/ `AUTO_REJECT`（新增）。
  `AutoApprovalRule.action` 直接复用 `REVIEW_APPROVE` / `REVIEW_REJECT`
  的值，避免「同义词漂移」。

### 2.2 数据模型

- `app/auto_approval/models.py::AutoApprovalRule` 增加字段
  `action: str = REVIEW_APPROVE`（带默认值，既有构造调用不破）。
  校验集中在保存路径，读取侧按 SQL CHECK 已保证合法。
- `app/db/database.py`：
  - `_auto_approval_rule_from_row` 读第 11 列；
  - `list/get` 的 `SELECT` 增加 `action`；
  - `save_auto_approval_rule(..., action: str = REVIEW_APPROVE)`，
    INSERT/UPDATE 都写该列；更新时不改 `version` 之外的既有语义
    （改动作同样 bump `version`，审计快照照旧能指认当时的值）。

### 2.3 迁移

新增 `app/db/migrations/024_auto_approval_rule_action.sql`：

```sql
-- 自动审批规则的「动作」：APPROVE=自动通过（现状），REJECT=自动驳回。
-- 默认 APPROVE 让既有规则与旧调用方的行为逐字不变。
ALTER TABLE auto_approval_rules
    ADD COLUMN action TEXT NOT NULL DEFAULT 'APPROVE'
    CHECK (action IN ('APPROVE', 'REJECT'));
```

迁移计数 `23 → 24`，`tests/integration/test_database.py` 的
`migration_count == 23` 断言同步更新，并新增一条「既有行回填为 `APPROVE`」的断言。

## 3. 求值与执行

### 3.1 匹配（不改）

`AutomaticApprovalService.matching_rule` 维持 `ORDER BY priority, id` 取第一条命中。
命中结果 `AutoApprovalMatch.rule.action` 就是这次决策的动作。这正是需求里的
「共用同一个规则池，按相同优先级处理」——通过规则和驳回规则在队列里互相竞争，
排在前面的先说话。

### 3.2 执行分派

`ReviewOrchestrator.apply_automatic_approval` 改名为
**`apply_automatic_decision`**（名字现在必须同时覆盖两个动作，沿用旧名会撒谎）。
仍返回 `bool`：有一条规则命中并且决策落地为 `True`；无规则命中、或落地被拒为 `False`。

```
match = await AutomaticApprovalService(db).matching_rule(candidate_id)
if match is None: return False
if match.rule.action == REVIEW_REJECT:
    try:
        await self.reject([candidate_id], AUTO_OPERATOR)
    except ReviewError: return False          # 与 approve 一样，拒绝即跳过
    await db.record_review_action(candidate_id, "AUTO_REJECT", AUTO_OPERATOR, {
        "rule_id", "rule_name", "rule_version", "dsl_snapshot",
        "condition", "conditions", "metadata", "download_job_ids": [],
    })
    return True
# 否则完全沿用现有 approve_and_enqueue + AUTO_APPROVE 审计
```

要点：

- **复用 `self.reject` 而不是直接改状态**：状态机校验、`REVIEWABLE_STATUSES`
  门禁、审计写入都在既有路径里，自动驳回与人工驳回因此不可能对状态产生分歧。
- `record_review_action` 的 details 与自动通过保持同一快照形状（`download_job_ids`
  对驳回为空数组），时间线/排查对两种决策读同一组键。
- 审计动作新增 `REVIEW_AUTO_REJECT = "AUTO_REJECT"`（`app/review/models.py`），
  与 `REVIEW_AUTO_APPROVE` 并列，注释写明「都不是人工动作」。
- 旧名 `apply_automatic_approval` 不保留别名：改名一次性改掉两个调用点
  （`sweeper.py`、`candidates.py`）和测试里的 fake，避免两套名字长期共存。

### 3.3 扫描器计数

`AutoApprovalSweeper.sweep_once()` 现在把返回值解释为「本轮通过几条」。改为
「本轮决策几条」并按动作分别计数，日志拆成
`auto_approval_sweep_completed approved=%d rejected=%d scanned=%d`，
让运营者从日志就能看出驳回规则是否在跑。返回值为两者之和，现有「== 1 / == 2 / == 0」
的断言语义不变。

## 4. 界面与交互

### 4.1 编辑器（`settings/_auto_approval.html`）

- 「优先级」旁新增「动作」下拉：`自动通过` / `自动驳回`，默认前者。
- 编辑已存规则时按 `edit_rule.action` 回填。
- 无 JavaScript 时下拉照常提交，服务端不依赖脚本。
- 保存时服务端校验动作属于白名单，非法值返回 400 而不是撞 SQL CHECK 报 500。

### 4.2 已保存规则列表

- 每条规则头部动作徽章：`自动通过`（active 色调）/`自动驳回`（muted 色调），
  与既有的 `启用/停用` 徽章并列。仅靠 DSL 文本无法一眼看出规则会做什么。
- 「试跑此规则」/「命中候选」保持；试跑结果文案按动作分叉——
  「将会自动通过 / 将会自动驳回」——否则一个驳回规则的试跑结果会读成它会把候选加进下载队列。
- 页首提示改为：命中「通过」规则自动加入下载队列；命中「驳回」规则自动驳回；
  **两类规则共用一条优先级队列，排在前面的先决定**。想先驳回就给驳回规则更小的优先级。

### 4.3 试跑（dry run）

`AutomaticApprovalService.dry_run` 增加可选 `action` 入参并写入
`AutoApprovalDryRun.action`，`app/api/serializers.py::auto_approval_dry_run`
透出 `action`；路径页的试跑不传动作，默认 `APPROVE`，其渲染不受影响。
试跑仍然只读、不写、不触发下载。

### 4.4 时间线

- `app/api/status.py::REVIEW_ACTION_STATUS` 增
  `"AUTO_REJECT": _view("AUTO_REJECT", "自动驳回", TONE_MUTED)`（与 `REJECT` 同 muted）。
- `app/api/serializers.py::_review_reason` 的自动规则分支改为
  `action in {REVIEW_AUTO_APPROVE, REVIEW_AUTO_REJECT}`，两种决策都显示
  「命中规则「X」」。
- 时间线上自动驳回与自动通过一样会先出现一条底层 `REJECT`（`operator_name=自动审批`）
  再出现 `AUTO_REJECT`，与自动通过今天的 `APPROVE` + `AUTO_APPROVE` 两行形态一致；
  这是既有形态，不在本阶段改动。

### 4.5 API 序列化

`app/api/serializers.py::auto_approval_rule` 增加 `"action"` 与 `"action_view"`，
`action_view` 来自 `app/api/status.py` 新增的 `RULE_ACTION_STATUS`
（`APPROVE → 自动通过 / ACTIVE`，`REJECT → 自动驳回 / MUTED`）。
`GET /api/v1/settings/auto-approval` 因此自带动作词汇，客户端不必自己拼中文。

## 5. 优先级语义与恢复路径（文档要写清）

- 一条候选只被**一条**规则决策，由 `(priority, id)` 最小者胜出，与动作无关。
- 典型配置：`priority=10` 的驳回规则（如 `TAG EXISTS NTR`），
  `priority=100` 的全通过规则——先剔除、再放行。
- 误伤恢复：自动驳回把候选置为 `REJECTED`，落在「已驳回」tab；
  停用/修正规则后，行内「重新排队」即可回到待审核，不需要手工改库。
  这是驳回动作可被接受的底线，页面提示会写明。

## 6. 备选方案与取舍

| 方案 | 结论 |
|------|------|
| 独立「驳回规则」表 / 独立优先级 | 否。运营者明确要求共用一个规则池、同一优先级。 |
| 全局「驳回模式」开关 | 否。无法表达「先驳回 A、其余通过」这类混合规则。 |
| 先全部自动通过、再跑一次驳回过滤 | 否。会产生「先入下载队列再撤回」的竞态与脏任务。 |
| 一条规则里同时写通过条件与驳回条件 | 否。一条规则只能有一个优先级与一个动作，塞两个会逼出第二套优先级。 |
| 驳回也保留旧方法名 `apply_automatic_approval` | 否。名不副实，两个动作却只有一个名字是下一次维护的坑。 |

## 7. 测试计划

基线当前 `1743 collected / 0 failed`，预计 +12～15。

单元 / 集成（`tests/unit/test_auto_approval.py`、
`tests/integration/test_auto_approval_workflow.py`、`test_database.py`、
`test_settings_web.py`、`test_work_detail_web.py`）：

1. 数据库往返：新建规则不传动作 → 存 `APPROVE`；传 `REJECT` → 存 `REJECT`；
   非法动作被拒。
2. 迁移：计数 `24`；旧库升级后既有行 `action='APPROVE'`。
3. `matching_rule` 返回带动作的规则；禁用规则不参与。
4. 优先级：`priority=10` 的驳回规则压过 `priority=20` 的通过规则（候选变 `REJECTED`、
   **无下载任务**、写 `AUTO_REJECT` 且快照含 rule_id/version/metadata）；
   反过来通过规则在前则走通过。
5. 扫描器：`sweep_once` 无人在线时把命中驳回规则的候选驳回，返回值计入；
   日志字段 `approved/rejected` 正确。
6. 待审核页渲染的延迟路径同样执行驳回。
7. 网页保存：动作下拉提交 `REJECT` 后列表出现「自动驳回」徽章；非法动作 400。
8. 试跑：驳回规则试跑文案为「将会自动驳回」，仍不写库。
9. 时间线：`AUTO_REJECT` 渲染为「自动驳回」并带「命中规则「X」」。
10. 回归：现有自动通过用例（不传 `action`）逐字不变。

## 8. 文档同步（实现时一并提交）

- `README.md`「审核先行」条目：写明规则分自动通过 / 自动驳回、共用一个规则池与优先级。
- `docs/USAGE.md`「自动审批规则」：新增「动作」说明、优先级竞争语义、误伤后
  「重新排队」恢复路径、试跑文案区别。
- `AgentHelp/progress.md` 追加 R58 条目（版本/测试基线/迁移编号 `024`），
  `AgentHelp/PHASES.md` 补一行 R58 索引；基线链从 R57 1743 更新。
- 环境变量、`.env.example`、页面名表格不受影响（无增删），提交说明里注明。

## 9. 待运营者确认

1. **动作命名**：界面用「自动通过 / 自动驳回」是否合适（还是「通过 / 驳回」）。
2. **驳回要不要记原因**：默认只在审计里记规则名，候选的 `filter_reason` 置空；
   若要「已驳回」列表直接显示命中规则，需要给 `reject_candidate` 增加 note 入参
   （改动更大，默认不做）。
3. **是否需要额外的保存确认**：驳回是自动、不可即时撤销（可重新排队），
   方案默认不加二次确认，只靠动作徽章 + 试跑提示。是否要一个保存前确认。
4. **扫描器日志/返回值语义**：确认改为「决策总数」并按 `approved/rejected` 分别计数。
