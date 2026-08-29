# 自动审批规则方案（待确认）

## 1. 目标与边界

自动审批只处理已经完成元数据补齐、仍处于 `PENDING_REVIEW` 且存在可下载来源的候选。规则命中后执行与人工“通过”相同的操作：记录审核日志，并幂等加入 Telegram 或 ExHentai 下载队列。

本文件只定义方案。本阶段不实现规则解析、存储、界面或自动执行。

## 2. 推荐表达式

字段使用 `{FieldName}`，值和逻辑运算使用受限 DSL，不允许执行 Python、SQL 或模板代码。

```text
({LanguageRaw} = "chinese" OR {Language} = "中文")
AND {CategoryRaw} = "Doujinshi"
AND {TAG} HAS_ALL ["language:chinese", "female:full color"]
AND {Rating} >= 4.0
```

第一版建议支持：

- 文本：`=`、`!=`、`CONTAINS`、`STARTS_WITH`
- 数值：`=`、`!=`、`>`、`>=`、`<`、`<=`
- 集合：`HAS`、`HAS_ANY`、`HAS_ALL`
- 空值：`EXISTS`、`NOT_EXISTS`
- 组合：`AND`、`OR`、圆括号
- 模糊匹配：`Like % _ `

不支持正则表达式、任意函数和字段间计算，先保持规则可解释、可预览。

## 3. 字段取值

普通 `{FieldName}` 使用候选的“生效值”：

1. 人工编辑值
2. Telegram 明确解析值
3. ExHentai 元数据值
4. 文件名或其他推断值

字段名沿用现有元数据名，例如 `{Title}`、`{Artist}`、`{Language}`、`{LanguageRaw}`、`{Category}`、`{CategoryRaw}`、`{Rating}`。

字段不存在时，普通比较返回 `false`；必须使用 `NOT_EXISTS` 才能显式匹配缺失字段。

## 4. `{TAG}` 默认来源

`{TAG}` 是专用集合字段，不等同于字符串 `{Tags}`：

- 默认合并 `Tags` 与 `TagsRaw`，按逗号拆分、去空格、忽略大小写并去重。
- 中文标签与上游 `namespace:value` 原文都可命中，避免翻译更新导致规则失效。
- `{Tags}` 和 `{TagsRaw}` 仍可用于精确检查展示值或原始值。

示例：

```text
{TAG} HAS "female:big breasts"
{TAG} HAS_ANY ["全彩", "female:full color"]
{TAG} HAS_ALL ["language:chinese", "原作"]
```

## 5. 规则配置界面

推荐使用条件构造器，而不是让用户直接输入整段表达式：

- 每行选择“字段、运算符、值”。
- 支持添加条件组，并选择组内 `AND` / `OR`。
- 页面同步显示生成的 DSL，便于复制和审计。
- 提供“预览命中”按钮，只列出匹配候选，不执行审批。

保存时持久化结构化 JSON AST，同时保存可读 DSL 快照。服务端只解释 AST，不使用 `eval`，也不拼接 SQL。

## 6. 执行与审计

- 规则包含名称、启用状态、优先级和版本号。
- 元数据补齐后按优先级评估；第一条命中的自动通过规则生效。
- 只提供 `AUTO_APPROVE`，不自动驳回。未命中或字段缺失的候选留在人工审核队列。
- 下载源不可用、元数据抓取失败或规则解析失败时保持人工审核，不降级为自动通过。
- 审核日志记录规则 ID、版本、命中的条件、当时的字段快照和下载任务 ID。
- 重复评估依赖现有下载幂等键，不产生重复任务。

## 7. 建议确认项

1. `{TAG}` 是否按上述方案同时匹配中文标签和 `TagsRaw` 原文。
2. 第一版是否只允许自动通过，不提供自动驳回。
3. Telegram 元数据格式确认后，是否增加来源限定写法，例如 `{Title@TELEGRAM}`。
4. 多条规则同时命中时，是否采用“优先级最高且第一条命中即停止”。
