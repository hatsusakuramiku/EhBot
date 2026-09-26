# AI 供应商与模型：重写方案（对齐 AstrBot 的管理方式）

> **状态**：已按运营者答复实现（R31，2026-09-26），代码、测试、文档同批落地。R28–R30 交付的那一版
> AI 供应商管理（迁移 018、五张表、`app/ai/*`、设置 → AI 页面）被运营者判定「AI 对接部分有大问题」，
> 本方案是对这一块的**重写**：接口形状按 AstrBot 的两层管理（供应商来源 + 模型）重做，并修掉下面
> 第 0 节列出的硬伤。
>
> **实现与方案的偏差（以代码为准）**：
> - 第 4 节「可配置的硬门槛」被运营者否掉：R31 **没有任何验证门槛**，`ai_require_verified` 设置键
>   与 `AI_MODEL_UNVERIFIED` / `AI_MODEL_DISABLED` / `AI_PROVIDER_DISABLED` 的抛出门一起取消，
>   保留「测试」按钮与「未验证」徽标。第 8 节四个问题的答案见下文标注。
> - `proxy` 字段**没有实现**：httpx 直接认 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量，单独一个入库字段
>   徒增一份要同步的真相。请求头由 `custom_headers` 覆盖。
> - `extra_body` 接受**任意**额外字段，而不是只放行 `max_completion_tokens`；仅 `model` / `messages`
>   / `stream` 被拒（由本服务掌管）。`temperature` 限 0–2、`max_tokens` 必须为正整数。
> - 迁移 019 **没有**给已有供应商预填 `{"temperature":0.2,"max_tokens":900}`（第 6 节的「建议预填」未
>   采纳）：默认请求体改为只发 `model` 与 `messages`，因此升级后路径答案指纹会变、第一次默认重排会
>   把书库判为「路径有变动」——这是运营者已知情并接受的一次性代价。
> - 方案里没写、但运营者后来追加的**「全局默认模型 + 页面级覆盖」**：AI 页拥有全局默认链，路径页用
>   `ai_settings.ai_model_source` 选「跟随全局默认 / 本页单独指定」；`ai_model_chain` 因此加了 `scope`
>   并进入主键。第 8 节的答案：① 不要门槛；② 留空按最兼容默认（接受指纹变化）；③ 要「备注:密钥」
>   批量格式与自定义头，不要代理字段；④ 适配器仍只有 OpenAI 兼容，用 `custom_headers` + 任意
>   `extra_body` 兜住网关差异。

## 0. 为什么重写：现版的具体缺陷

不是「不好看」，是**配不上**：下面第 1、2 条会让一部分供应商/模型**永远无法进入模型链**，第 3–5 条
让能配置的也要花十倍手数。每条都指到现版代码。

| # | 缺陷 | 现版行为（证据） | 后果 |
|---|------|------------------|------|
| 1 | **请求体写死 `max_tokens` + `temperature`** | `app/ai/client.py` 的 `complete()` 无条件构造 `{"model", "messages", "max_tokens", "temperature"}`，验证请求同样（`app/ai/service.py` 传 `VERIFY_MAX_TOKENS` / `temperature=0.0`） | OpenAI 推理模型（o1/o3/o4-mini/gpt-5 系）明确**拒绝 `temperature`**、并要求用 `max_completion_tokens` 代替 `max_tokens`；部分网关对未知/不支持参数也直接 400。这些模型**验证必失败 → 永远进不了链**，页面显示「AI 供应商拒绝了这次请求（HTTP 400）」加上供应商原话，但没有任何一处提示「是参数的问题」 |
| 2 | **验证是硬门槛，且只能一个个来** | 未验证/最近失败 = 不能进链（`ai_provider_models.last_verify_ok`）；每次验证是一次**真实付费调用** | 端点临时抖动、或第 1 条命中时，整条链配不出来；拉取 30 个模型要付费 + 点 30 次 |
| 3 | **API Key 只能逐把添加** | `add_ai_provider_key` 一次一个表单 | 有 10 把 Key 就要提交 10 次 |
| 4 | **模型只能逐条添加/启停** | 「添加模型」一次一个名字；`toggle_ai_provider_model` 一次一个 | 拉取到几十个模型后没有「批量勾选/批量启用」 |
| 5 | **400 不分类** | `_classify_status` 的非 401/404/429/5xx 全部落 `AI_REQUEST_REJECTED` | 「参数不被接受」和「配置写错了」被说成同一句话，把人引向错误的排查方向 |
| 6 | 无代理 / 自定义请求头 | 表格里没有这两个字段；AstrBot 的来源模板两者都有 | 需要走代理或网关带自定义头的部署无法接入 |
| 7 | 推理模型的输出没有专门处理 | `extract_json_object` 找「第一个 `{` 到最后一个 `}`」 | `<think>` / `reasoning_content` 里出现花括号时可能抓错片段 |

## 1. 参考对象：AstrBot 的两层管理

AstrBot（`AstrBotDevs/AstrBot`，本方案按 `main` 分支核对）把「模型供应商」拆成**两层**，这也是本次
重写的骨架：

- **供应商来源 `provider_sources`**（`astrbot/core/config/default.py`）：一条 = 一个端点 + 凭据。
  字段是 `id` / `provider`（厂商）/ `type`（协议适配器）/ `enable` / `key`（**数组**）/ `api_base` /
  `timeout` / `proxy` / `custom_headers`，外加适配器专属项。
- **模型 `provider`**：一条 = `provider_source_id` + `model` + `enable`，并可带 `model_config`
  **逐模型覆盖参数**。模型列表由来源的 `GET /v1/models` 拉取（`/provider-sources/{id}/models`）。
- **主力与备用是 id 列表**：主力 `agent_runner.config.model.provider_id`，备用
  `fallback_provider_ids`（有序）。不搞「在一个列表里上下移动」。
- **Key 是来源上的一个数组**：失败时从候选里剔除并换下一把（`openai_source.py` 的
  `available_api_keys`），随机轮换。
- **来源级别的操作**：启用开关、测试、编辑、删除；模型级别才有「拉取/测试/启用」。

**AstrBot 对我们真正有价值的两点**：请求参数按模型可覆盖（它默认**不发** `max_tokens`，见其
`openai_source.py` 注释 #9206）；「能不能用」和「配没配好」分开——配置不阻塞，测试是按钮。

我们的盘子本来就已经是两层（`ai_providers` / `ai_provider_models`），所以重写不是推倒表结构，而是
**把管理方式换成上面这套**，并把第 0 节的硬伤修掉。

## 2. 目标形态（一屏看懂）

```
设置 → AI
├─ 供应商（表）
│   名称            编码     基础地址                  Key(可用/总)  模型(启用/总)  启用  操作
│   OpenAI 主力     openai  https://api.openai.com/v1     3/3        2/40       [x]   编辑 测试 拉取模型 删除
│   Local LM Studio openai  http://127.0.0.1:1234/v1      1/1        1/3        [x]   编辑 测试 拉取模型 删除
│   └─ (展开) Key 管理：一次性粘贴多把；每把显示 状态/冷却至/最后使用；可启用·停用·删除·重置冷却
│   └─ (展开) 模型管理：[拉取模型] → 勾选批量添加；[批量启用][批量停用]；每行：名称 | 启用 | 验证 | 操作
│                                                                        操作 = 测试 · 设为主力 · 加入备用 · 移出
├─ 模型链
│   主力： OpenAI 主力 / gpt-5           [换主力…]
│   备用 1： Local / qwen2.5-7b-instruct  [上移][下移][移出]
│   备用 2： …
└─ [全部测试] 顺序或并发 N 测试当前供应商的模型
```

「设为主力」「加入备用」这两个动作取代现版的「加入模型链 + 上移/下移」——**改变顺序的入口只有一个**
（就是备用列表本身），因为「第 0 位是主力」这件事必须只有一个真相来源。

## 3. 请求形状：修掉缺陷 1、5、6、7

**参数合并（四层，后者覆盖前者）**：

1. 代码默认：**不发** `temperature`、**不发** `max_tokens`（AstrBot 同款保守默认，最兼容）；
2. 供应商级默认参数（页面上可填，留空=不发）；
3. 模型级覆盖（每个模型一个「高级」折叠：`temperature`、`max_tokens`/`max_completion_tokens`、
   额外请求体 JSON）；
4. 调用点参数（验证用 `max_tokens=64` 这种硬需求）。

额外请求体（`extra_body`）是逃生口：不同厂商要 `reasoning_effort`、要 `max_completion_tokens`、
要禁 `temperature`，都在这里表达，不必为每家写一个适配器。

**错误分类**：新增 `AI_PARAM_REJECTED`。当 400 的供应商原文命中
`temperature|max_tokens|max_completion_tokens|unsupported|unknown parameter` 时归入此类，页面文案
直说「该模型不接受这个参数，请在模型高级设置里关闭/替换」，而不是把供应商原话丢给运营者猜。
其余分类保持不变（401/403 换 Key、429 冷却换 Key、404 换模型、5xx 重试、超时重试）。

**推理输出**：解析前先剥掉 `<think>…</think>` / `<thinking>…</thinking>`；如果响应里存在
`reasoning_content`，只用 `content`，绝不在推理文本里找 JSON。

**代理与自定义头**：来源上新增 `proxy`（http/socks 代理 URL）与 `custom_headers`（JSON 对象，值不回显）；
`httpx` 客户端按来源构造。

## 4. 验证与联通性：可配置的软门槛

- 保留「验证」这个动作与 `last_verified_at/ok/error` 三列，页面对每个模型显示
  `已验证 / 未验证 / 已失效（时间 + 原因）`；供应商行显示 `已验证 x/y`。
- **新增开关 `ai_require_verified`（设置 → 路径 的 AI 面板，默认关）**：
  - 关（默认，AstrBot 式）：未验证的模型**可以**进链，只是行上带一个「未验证」徽标；
  - 开：恢复现版语义，未验证/最近失败拒绝进链（拒绝时说明原因）。
- 「全部测试」：对某供应商（或整库）的启用模型顺序/并发执行一次真实调用，结果就地更新；**并发数**复用
  路径设置里已有的 `ai_concurrency`。
- 拉取的模型列表**不落库**，勾选后批量写入（已存在的不重复建）。

## 5. Key 管理：修掉缺陷 3

- 新增 Key 用 **textarea，一行一把**（可选 `备注:密钥` 形式），一次提交批量加密入库；
- 列表每行：`备注` / `状态（可用 · 冷却至 HH:MM · 停用）` / `失败次数` / `最后使用` / 操作
  （启用·停用·删除·重置冷却）；
- 排序按「可用在前」，让运营者一眼看到还剩几把能用。

## 6. 数据模型：迁移 019

表结构**不推倒**，只加列 + 一个设置键：

- `ai_providers`：+ `proxy TEXT NOT NULL DEFAULT ''`、+ `custom_headers TEXT NOT NULL DEFAULT '{}'`、
  + `default_params TEXT NOT NULL DEFAULT '{}'`（供应商级默认参数 JSON）。
- `ai_provider_models`：+ `params TEXT NOT NULL DEFAULT '{}'`（逐模型覆盖 JSON）。
- `ai_model_chain`：语义不变（`position` 0 = 主力，其余备用），只是**入口换了**。
- `ai_provider_keys`：唯一改动是**批量写入**，无列变更。
- 设置键：+ `ai_require_verified`（bool，默认 0）。
- `ai_path_suggestions`（答案缓存/指纹）**完全不动**——它跟管理方式无关，且 R30 的重排逻辑依赖它。

升级路径：018 → 019 是纯加列，旧库的提供商/Key/模型/链原样保留；因为默认「不发参数」与现版「总是发
`max_tokens=900`+`temperature=0.2`」不同，升级后**路径答案的指纹会变**（prompt 没变、但请求参数变了），
第一次默认重排会把整个书库判为「路径有变动」。这是需要运营者知情的一次性代价（可选：迁移时把
`default_params` 预填成 `{"temperature":0.2,"max_tokens":900}` 保持指纹不变——**建议这样预填**，
`ai_require_verified` 默认关则会带来「未验证也能进链」，两者都不改变已有答案的有效性）。

## 7. 落地顺序与验收

1. 迁移 019 + 参数合并 + 请求构造 + 错误分类（`app/ai/client.py`、`models.py`）——纯后端，单测覆盖；
2. 服务层：批量 Key、批量模型、批量验证、设为主力/加入备用（`app/ai/service.py`、`db/database.py`）；
3. 页面重写 `app/web/templates/settings/_ai.html` + 路由（保持现有 URL 语义，新增的批量端点；
   旧端点保留到页面不再引用为止，避免书签/脚本一次性失效）；
4. 文档同步：`README.md`、`docs/USAGE.md`、`AgentHelp/EHBot.md` §4.6、`AI_PATH_PROPOSAL.md` 状态行、
   `progress.md` 追加 R31 条目；
5. 测试：`tests/unit/test_ai_providers.py`（参数合并/分类/推理输出）、`test_ai_paths.py`（链语义不变）、
   `tests/integration/test_settings_web.py`（批量 Key/模型、设置主力、全部测试）。

验收标准：① 一个只接受 `max_completion_tokens` 的推理模型能验证通过并进链；② 一次粘贴 10 把 Key 只提交
一次；③ 拉取 40 个模型后能一次勾选批量启用；④ 已在链中的模型「设为主力」一击生效；⑤ 默认重排的范围
与 R30 完全一致（指纹问题按第 6 节处理）。

## 8. 需要拍板的四个问题（已拍板，见文首「实现与方案的偏差」）

1. **验证门槛**：按第 4 节改成「默认放行 + 可开关的硬门槛」，还是保持现版「未经我验证不得进链」？
   （建议前者，理由见第 0 节第 2 条。）
2. **参数覆盖的默认值**：迁移时把现有供应商预填 `{"temperature":0.2,"max_tokens":900}`（指纹不变、
   代价是仍挡推理模型），还是留空按最兼容默认（指纹会变，第一次整库重排）？（建议前者，且推理模型
   由运营者在该模型的高级设置里单独关参数。）
3. **是否要 Key 的「备注:密钥」批量格式 + 代理/自定义头**？（建议都要；代理/自定义头是 AstrBot 来源
   模板里就有的字段。）
4. **协议适配器是否仍只有 openai 兼容**？（建议维持；但 `custom_headers`+`extra_body` 要能兜住
   网关差异，这也是 AstrBot 用适配器解决的问题里最常见的一类。）
