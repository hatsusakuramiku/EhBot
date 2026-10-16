# P10 / R97 全域 API / 多端并发完成核对（2026-10-16）

## 最终验收（本节覆盖后文各次进行中快照）

源码 rc13/schema039，3088 collect-only。152份JUnit按当前完整节点及报告mtime取最新：1623个不同相关用例全部通过，0failure/error/skip；未跑整库。集中P10合同与旧设置/API/身份/账号/队列/实时612/0（765.424秒），WS取消及维护四文件76/0（89.668秒），真实HTMX/Alpine root及/ehbot通过。三类手动来源与旧关联/038迁移/详情83条最终82pass/1fail（95.146秒），失败是跨秒的预览Added；固定测试时钟后完整详情39/0（75.949秒）。各组重叠，不相加。

| 范围 | 最终实现与裁决 | 验收 |
|---|---|---|
| 元数据、审核、下载/打包、移除 | WorkCommands/共享应用层，同事务身份/版本/意图/回执；固定产物，锁外网络后复查；旧Web和JSON同核心 | 通过 |
| 已下载、改名、去重、手动添加 | 固定批次范围/逐件结果，真实移动journal，原作品删除/编号复用不接管新作品；手动网络UNKNOWN不重发 | 通过 |
| 设置、规则、profile与工具安装 | 独立保存区/条目版本，实际旧写者及回执同事务；安装持久受理、OS锁、真实线程收束与中断恢复 | 通过 |
| AI供应商/Key/模型/链/参数与网络 | 稳定身份、独立配置版本；网络捕获原配置/凭据、独立claim、断线继续、重启UNKNOWN；过期拉取清单拒绝保存 | 通过 |
| 密码库与API Key | 安全HMAC回执、四入口共享；Key明文仅首次响应，无跨会话全局槽；迟到解压健康不触碰替换密码 | 通过 |
| 维护 | 固定全库范围/作品版本/精确输入与逐件回执；GET只读/原键显式续作；缓存重放不删除新答案 | 通过 |
| 画廊/磁力/预览人工修改 | 三PUT JSON/WS与旧表单共享SourceCommands；合并另核对目标UUID/revision，等待停止期间目标变化固定失败且不可旧意图重试 | 通过 |
| v2、旧映射、WS与草稿 | 稳定task URL、当前能力/安全快照，v1共用领域服务；原生mobile/desktop支持api/v2；原条件/草稿保留，确认才换版本和键 | 通过 |
| 真实038升级/回滚 | 039失败DDL整体回滚；旧凭据/缓存/UUID保留；旧reference guard=NULL，重复初始化保留新guard；027–038无修改 | 通过 |

集中首轮584条582pass/2fail：一条在WS上下文退出抛CancelledError，修前确定性复现；socket finally屏蔽取消并等待子任务退出后76/0及612/0。另一条维护并发测试无条件要求两次立即成功，现按已定合同允许PREPARED期间可重试409，并验证退出后同键结果一致、只移动一次。来源首轮23条20pass/3fail包含测试FK清理遗漏和retry guard放错位置，均修复；公开67条64pass/3fail是parser仅读hidden，改完整读取来源文本字段。83条曾全部通过但默认pytest临时目录清理权限导致进程exit1，改本任务basetemp后暴露并修复前述跨秒夹具。失败报告保留至交付留痕后精确清理。

正式测试均在tests，不建.scratch；compileall、CodeGraph和标准diff-check通过。README/USAGE/REALTIME_API与接手文档同步，无环境变量变化。039随整阶段本地提交冻结；立即审查暂存diff并核实SHA后才进入P11，不拆切片提交。models.py正式字段纳入，仅排除既有BOM。P11仍处理审批LIMIT饥饿、ComicInfo长写锁、缩略图PENDING竞态与pending同步公平；P12处理最终合同与Linux guardian实跑。没有真实供应商/真实库升级/推送/发布/部署，总目标active。

## 当前接续：维护入口与原生 v2（覆盖下文待实现项）

全库重新归档、AI 路径缓存清理、路径规则试跑、模板预览已接共享 JSON/WS/旧表单。RearchiveCommands 用既有批次/逐件回执和 relocation journal，规划前后核对 rearchive 范围版本，固定作品 UUID/revision、精确输入产物、路径策略与逐件 AI 答案。规划只读本地数据，不隐式拉 EH 元数据或询问模型；需网络的处理交给后台任务。逐件提交再核对，配置/作品/答案变化保存拒绝；删除后数字编号复用不能移新作品。pin/入队/回执同事务，移动最终提交失败恢复原 CBZ。路径无效只在实际子命令提交时停车 WAITING_PATH，试跑不停车。

缓存独立 path-cache 版本，DELETE rowcount 不计算触发器的版本更新；回放原清理返回固定 removed，不删除后来的答案。039 触发器让作品事实/路径策略/缓存变化推进维护范围，进度/心跳不推进。实际 038 升级、DDL 回滚与重复 initialize 保留旧缓存/凭据及新 scopes 已验。重新归档 GET ?idempotency_key 只查已提交结果：INCOMPLETE 不意味着后台仍运行，不自动执行，明确原键重发续作。文件移动 PREPARED 时重复请求409 PUBLICATION_RECOVERY_REQUIRED，不把临时阻塞冻结为永久子项失败；受控真实线程交错通过。125 件固定范围全部入队，受理后新增作品不混入。

维护表单共享 settings_command_fields，data-settings-action 保留广播草稿及原版本/key，明确确认才采用新条件。修正模板宏误名造成三个失败，以及规则试跑测试 AST 误用 type/op 合同造成一个失败。core/HTTP/038/原规划四文件63/0，旧维护页面12/0；扩展受控交错/维护/038/v2/WS五文件最终57/0（66.265秒），归档/文件恢复/设置/DB/原规划七文件214/0（125.790秒），root及/ehbot真实DOM通过。首轮扩展17/1是测试复用WS request_id，按独立request_id修复；原生组55/1是旧合同仍断言仅api命名空间，已改成api/v2并最终通过。

原生 WS 原先只过滤 api.*，导致 v2.* 不可用。现在 mobile/desktop 共用 api.*、v2.*，仍无 web.* 与 reveal；Bearer 原生任务控制→旧HTTP回放已验。README/USAGE/REALTIME_API 已同步。3071 collect-only；142 XML按当前完整节点与报告mtime取最新1579 distinct passed、0failure/error/skip，非整库。P10所有新增正式合同及旧设置/API/身份/账号/队列集中回归正在运行，尚未验收/提交；rc12/039保持实验，027–038不改，HEAD=f60412b。下一步收尾该实际测试句柄、全阶段差异/合同审核后才验收提交；P11/P12未开始，总目标active。

## 当前接续：v2 与全域入口复查（2026-10-16）

新增 `/api/v2` 任务列表/详情、cancel/retry/pause/resume/switch-source/stop-seeding/priority、作品下载/精确产物打包/remove/purge、独立操作查询和账号原函数别名。v1、v2、WS共用领域命令和固定回执；任务UUID固定在路径中，历史移除后仍可查询。列表与详情的多SQL在同一读事务内，响应过滤provider原始载荷/资源地址。capabilities区分supported/configured/available，读取账号注册表/安全运行状态和qB配置，不把服务实例存在误报为已配置。明确运行暂停不支持、切换目标仅EH原档/Telegraph、WS accepted帧非持久受理、无replay以及Key明文仅首次响应。

活动相同来源和打包意图复用当前任务；不同打包意图409，不静默修改。另一支持来源通过既有QueueControl停止操作切换，预检在锁外；提交复核原task/版本与来源引用，操作/作品版本/回执同事务。原任务STOPPING期间无新路径，真实停止确认后才入队目标，pack_requested随操作传递。失败/并发/丢响应回放与旧API共享结果。打包任务明确不允许切下载来源。

HTTP Idempotency-Key通过deps.command_payload归一为WS同名body字段，覆盖作品/队列/设置/账号/摄取/已下载；头体不同409。API操作者改从唯一管理员实际身份读取，修复改名后的Bearer审计仍写admin；Web Cookie由现有validate_session逐请求刷新username，未再添加平行身份机制。

已删除经索引和全app/tests限定搜索确认不可达的torrent_verify注入、complete_if_ready及仅其使用的读取方法/import；保留原持久重试/停止确认流程。原v2相关六文件106/0（76.38秒）；扩展入队/串行切源三文件19/0（14.710秒）。首轮新核心两fail为测试误调process_pending_operations和复用edit幂等键，已按真实接口/独立键修正。v2/管理员/账号/队列五文件复验正在运行，不提前宣称通过。

**仍有P10缺口，不能验收提交：** 逐路由比对找到Web独有的全库重新归档、AI路径缓存清理、路径规则试跑和模板预览；重新归档仍直接逐件pin/入队/移动，缺全局所见范围/逐件CAS与固定批次结果。下一步补共享应用命令和JSON映射、真实冲突/中断/回放测试及表单草稿，之后才完成全域核验。039未冻结、rc12保持、HEAD=f60412b，P11/P12未开始；阶段验收后stage/review/local commit/核对SHA，不拆P10切片提交。

## 当前接续：密码库/API Key（覆盖后文历史快照）

CredentialCommands复用原DB写者和校验，短事务将CAS/写入/安全回执一起提交；加密和密钥生成在锁外，最终事务再次核对。039新增vault/api-key独立设置版本，密码UUID/编辑版本及内部写触发器。add保留按名称upsert；delete核对原数字ID/UUID/条目版本，删除或编号复用410。健康时间和认证touch不推进配置版本；解压输入同时捕获UUID/版本，迟到成功不会更新改过或重建的密码。

HTTP/WS/四个原表单共用核心，缺条件428、旧版本409、同键异内容409。回执只存安全身份/版本/动作，含密码输入的摘要用私有密钥HMAC。生成仅真正提交者的首次响应返回明文，重放固定安全回执，不旋转Key；提交后取消等待真实线程退出，重启仍可原键查回执。原生生成直接200显示本响应，删除app.state.pending_api_key和?reveal=1读取槽；另一个真实密码登录会话无法读取。丢响应须核对状态后明确新键/当前版本生成。API Key bearer不能生成或撤销Key，密码Cookie/access token可以。

冲突保留非秘密草稿、原UUID/版本/key；服务器不回显密码，实时提交仅恢复浏览器DOM属性，无JS重新填写。后台刷新在public_id未变时保留当前首次明文，Key变化/撤销或显式导航清除。root及/ehbot真实HTMX/Alpine已验证。

公开/旧设置四文件200/0（304.32秒），最新核心/真实038迁移/归档三文件46/0（22.93秒），相邻公开/打包取消/转换日志/数据库四文件43/0（14.75秒），分组重叠不相加；3039 collect-only，126XML按当前完整节点/mtime取最新1439 distinct passed，0failure/error/skip，非整库。首次公开3fail为测试夹具漏建可撤销Key、误用初始密码及解析器只读hidden，修夹具后通过；生命周期首轮55pass/1fail是旧表单测试缺版本，另修删除测试空操作假阳性后通过。

接续v2任务域/能力目录和旧映射、全域旧入口/草稿与不可达complete_if_ready/_torrent_verify清理裁决。P10未验收/暂存/提交，HEAD=f60412b；rc12、冻结027–038保持，039实验未冻结，P11/P12未开始。整阶段验收才暂存代码/正式测试/同步文档、审查暂存diff、本地提交并核实记录SHA，再进入P11；仅排除downloads/models.py既有BOM差异。Linux guardian实跑留P12，总目标active，无真实供应商/真实库升级/推送/发布/部署。

## 当前接续：AI 网络持久操作（覆盖下文历史快照）

AiNetworkCommands覆盖单模型、供应商全部、全局全部测试和模型列表拉取。039新增操作/claim表；短受理事务核对所见身份/配置/Key版本，捕获原provider/model及可用Key密文信封。网络在锁外，不执行时重读替换配置；普通操作表只保存安全身份/版本和结果。不同模型独立，同模型单项/批量互斥，拉取按供应商独立互斥。编辑版本不因测试消耗；全局测试保持原verify_all的所有启用模型语义，含停用供应商下启用模型。

HTTP/WS/原表单共用核心：同键同内容固定202和原ai_operation_id，先回放再查当前对象；异内容409，缺条件428，原对象消失/数字ID复用410，重叠409 AI_NETWORK_BUSY。受理commit→dispatch屏蔽客户端取消，停机拒绝新请求并等待已受理网络，重启RUNNING→UNKNOWN/释放claim，不自动重发。迟到模型验证同时核对原配置和操作/worker session；安全结果不存供应商原始错误。GET结果no-store/鉴权，旧表单303跳结果页，最近10项可重开，不再次调用供应商。固定中文说明取代原始错误文本；默认链为空也可测试整个模型目录。

拉取结果仅原provider UUID/revision/keys_revision仍一致时显示勾选。另复现页面展示后改Key/端点仍可提交的竞态：models-add新增discovery_operation_id，在同一保存事务再次核对已完成fetch的原配置，过期409 AI_DISCOVERY_STALE，不添加/不消费集合版本；错误页保留选择与原操作，禁止确认过期清单。已成功添加的回执仍优先重放。

最新网络/目录/038升级/worker/原AI单元六文件134/0（68.712秒），最后身份/HTTP/迁移三文件29/0（35.359秒）；旧AI页面与合同四文件42/0（57.812秒）、目录与HTTP三文件51/0（76.901秒），按节点最新结果，不相加。真实HTMX/Alpine root与/ehbot下，操作结果随广播刷新、已删除模型的原条件/Key/显式确认草稿保留均通过。compileall通过。3019 collect-only；120XML按当前完整节点/报告mtime取最新1408 distinct passed，零failure/error/skip，非整库。

初次新HTTP夹具漏必填label导致10error，修夹具后9pass/1fail暴露全局测试表单被默认链隐藏；修正条件后通过。拉取选择竞态先两条按预期失败（API200/原生303错误接受旧清单），修后通过。一次扩大验证误写不存在test_ai_service.py，未执行测试；改真实test_ai_providers.py后134/0。完整失败留痕见progress。

下一步密码库/API Key：旧PUT及原表单仍无所见版本/持久回执，API Key新建丢响应会再次替换；原生pending_api_key是跨会话全局槽，可被另端消费，须完整处理。之后v2能力目录、全域旧入口/草稿核验、不可达complete_if_ready/_torrent_verify清理裁决。P10未验收/暂存/提交，HEAD=f60412b，rc12/冻结027–038不变、039实验未冻结；P11/P12未开始，Linux guardian实跑仍留P12，总目标active。整阶段验收后再审查暂存diff、本地提交并核实记录SHA。

## 当前接续：路径 AI 参数、提示词与模型来源（覆盖下文历史快照）

SettingsCommands的path-ai复用原服务校验，将八字段先存SettingsDraft，再一次CAS/实际写入/安全回执；晚期校验失败和回执失败均不留部分修改。path-ai-prompt与面板共用path-ai版本；路径及候选模型来源各自使用path-model-source/candidate-model-source。内部实际字段修改同样推进对应版本。开启Series先保持原校验优先级，再在最终事务内复查当前总开关及按当前模型来源选出的链；准备期间关闭AI、删模型或切到空自定义链均整体拒绝，无网络/长期写锁。

四种动作已接HTTP/WS/旧表单；新增PUT /api/v1/settings/paths/ai/models补路径模型来源JSON入口。重置提示词采用独立非嵌套表单/独立key，错误草稿按action区分，重置冲突不会把面板其他字段渲染成空。面板保留radio/prompt/数字/checkbox与原版本/key，Series拒绝后可取消勾选重试。明确确认才更换版本/key，固定重放不清掉后来提示词。模型来源的JSON model_source和表单ai_model_source归一。

最新路径AI/设置/038升级五文件38/0（32.741秒），旧Web及JSON入口90/0（160.899秒），真实HTMX/Alpine root与/ehbot草稿/原版本/key保留通过；compileall通过。2991 collect-only，107XML按当前完整节点/报告mtime取最新1380 distinct passed，零failure/error/skip，非整库。初次核心8pass/4fail均为测试误调不存在的reader或default_params传dict，修正夹具后最终通过；DOM新增变量与既有sourceKey重名，修正后通过。027–038不改写、039实验未冻结，HEAD=f60412b，P10未验收/暂存/提交。

下一步验证/拉取模型的持久受理，包含单模型、供应商全部/全局全部验证及模型列表拉取全部旧入口。受理时固定原供应商/模型/凭据身份和配置，网络在锁外；客户端断线不取消已受理执行或重复发送；重启未完成操作UNKNOWN且不自动重发。之后密码库/API Key安全回执、v2能力目录与全域旧Web/API/WS/草稿核验。P11/P12未开始；整阶段验收后暂存代码/正式测试/同步文档，审查暂存diff、本地提交核实SHA，再进入P11，总目标保持active。

## 当前接续：Key/模型与三scope模型链（覆盖后文历史快照）

AiCatalogueCommands八种本地操作已接HTTP/WS/原表单：keys-add/models-add使用父集合版本；Key/模型启停、删除与模型参数使用独立UUID/revision；取消冷却额外核对health_revision，成功只消费健康版本，旧回执重放不会清掉后来冷却。原DB写者通过可选connection复用，CAS/写入/固定安全回执同事务；原纯校验提取复用，加密在锁外，Key内容以私有主密钥HMAC裁决。回执不包含明文/密文/参数。原对象删除或编号复用返回410；原成功回执仍可重放。

AiChainCommands为default/archive_path/candidate_admission分别使用settings_versions的ai-chain:<scope>。原服务排序提取chain_order复用，原DB整链保存增加外部事务接缝；身份/CAS/排序/保存/回执同事务，模型参数与健康不推进链版本，成员删除和旧内部写者触发scope版本。接受无变化也消费原版本；不要求连续+1。快照的ai-catalogue epoch不作编辑锁，paths/parse同时核对相应链版本和catalogue，避免旧链配新版本。页面下拉项直接携带编号/UUID；冲突页保留原动作、模型身份、版本/key，确认只更换链版本与新键。广播保留已消失对象草稿。Key错误页不回显api_keys，实时页面只在DOM属性保留当前输入；无JS须重新粘贴。

最终：合并七文件86/0（83.91秒）、排序及原AI单元93/0（19.71秒）、旧入口46/0（57.39秒）、相邻设置/API五文件80/0（95.18秒），加强参数不推进链版本1/0（0.48秒）。真实HTMX/Alpine的Key秘密/冲突控件/模型参数与链草稿在root和/ehbot均通过。真实038夹具验证新列回填、DDL失败整体回滚、重复初始化、原链保留与删除推进scope。2967 collect-only；103XML按当前完整节点及报告mtime取最新1356 distinct passed，0failure/error/skip，非整库。compileall/CodeGraph/diff检查通过；所有进程终态，暂存区空，冻结027–038未改，HEAD=f60412b，无.scratch。

**下一步**：路径AI参数/prompt/model-source保存区与原子回执；验证/拉取模型独立持久操作，禁止锁内网络/断线重发并捕获原始配置/凭据，重启UNKNOWN；密码库/API Key、v2能力目录、全域旧入口及草稿审计。后文“Key/模型/链未接”均是历史快照，本节为当前事实。P10未验收/暂存/提交，rc12保持、039未冻结；P11/P12未开始，Linux guardian实跑仍留P12。整阶段验收才本地提交并记录SHA，再进入P11，总目标继续active。

## 最新接续：手动新增与设置保存区（覆盖后文历史快照）

手动新增也已接持久请求：manual_work_requests 在原 P5 候选/元数据/下载意图发布事务保存成功回执。相同键并发或丢响应重发不重复网络，供应商拒绝固定 FAILED，取消/失效会话固定 UNKNOWN；HTTP/WS/旧表单共享结果。新增操作查询，广播保留链接/key；改链接须显式新建另一操作。Web 成功跳转核对稳定 work_id，原作品删除/编号复用返回410，API仍返回原成功回执。manual-contracts 35/0（33.112秒）、manual-http 10/0（5.671秒）、新增编号复用后的manual-final 10/0（5.51秒），组间重叠不相加。首次补验错误地按位置传删除参数，改为真实purge_work关键字调用后通过。设置/规则/AI/v2与全域草稿仍待完成，不视为阶段验收。

系统、解析、AI候选判定参数、安全限制、自动打包、命名模板、运行目录七个独立保存区已接 SettingsCommands。复用原服务验证，使用隔离的 SettingsDraft 缓冲写入，无网络/目录预检长事务；最终CAS、全部设置、版本与安全回执一次提交。触发器同时覆盖原内部写入，不相干维护字段不推进版本；无变化保存也消费所见版本。普通回执只有scope/revision/status，快照另读，读前后核对版本防止“旧值配新版本”。系统缓存提交后按序重读，回执重放可补偿未刷新。HTTP/WS/旧表单共享；页面保留原值/版本/key，模板预览也保留；显式确认用新版本/新键形成可重放新操作，广播保留原DOM。

settings-final-contracts三文件14/0（6.90秒），包含真正SQLite竞争/回滚、038升级DDL失败恢复、重启回执、目录预检时另一个写者、快照读竞争及HTTP↔WS↔表单。realtime/log DOM root/proxy exit0。旧入口首组251条37fail/0error（378.427秒），主要旧测试遗漏版本/仍取保存响应的快照，另有旧WS打包漏作品版本；正式用例已显式迁移为先读版本、保存、再读快照，未放宽产品门禁。settings-legacy-fixed与settings-runtime定向回归尚在运行，不能把旧结果作为最终通过。

**最终复验（覆盖上行“在跑”快照）**：settings-legacy-fixed180/0（305.041秒）、settings-runtime99/0（106.797秒），两会话已exit0。补齐服务器冲突/预览后表单data-dirty，并让错误响应真正换入确认控件，后续广播继续保留原草稿；手动表单同样补齐。settings-draft-final四文件16/0（9.32秒），realtime/log真实DOM新增“冲突→确认控件→再次广播”并在根路径与/ehbot通过；首次DOM旧断言期待“实时已连接”，当前应显示草稿保留提示，更新断言后通过。2829 collect-only，57XML按当前仍存在节点取最新共942 distinct passed、0failure/error/skip，非整库。所有工具会话终态；CodeGraph/diff检查通过。

**逐条规则已接通（覆盖后文旧缺口）**：039为审批/路径规则回填rule_uuid，原version同时作为逐条revision；新增和编辑/启停/删除通过RuleCommands，同事务CAS/原数据库保存/固定回执。数字编号复用拒绝旧身份，内部启停或实际内容变更也推进版本，不同规则不共享版本。列表同SQL读取内容/身份/版本；成功回执只含安全身份/版本/动作，快照另GET。HTTP/WS/原表单共享，冲突与试跑保留原输入/条件/版本/key，显式确认才采用当前版本；后台广播保留草稿和已消失规则的拒绝操作。复用原路径模板校验，不新增第二套校验。最终六文件78/0（33.678秒），旧页面等五文件237/0（315.219秒），DOM root/proxy exit0；2846 collect-only，63XML当前节点1015 distinct passed，0failure/error/skip，非整库。失败/修复见progress，本轮所有进程已终态。

**qBittorrent 配置接续**：第八个设置区torrent已接SettingsCommands与HTTP/WS/旧表单；同事务CAS、加密设置与安全回执，JSON字段省略保留原值、空密码保留，拒绝掩码。敏感内容使用私有主密钥HMAC，回执不含秘密/裸摘要。主密钥生成通过进程共享线程锁覆盖原服务及准备副本，首次并发不再替换彼此的密钥。冲突保留非敏感草稿与原版本/key，服务器不回填密码，浏览器只在DOM属性保留当前密码并采用服务器的新确认控件，后续广播保持草稿。

运行核查：push与poll各自捕获不可变config快照；原poller已有instance/hash/attempt标签核对。新增真实MockTransport交错测试证明推送在途改实例仍仅请求原实例、原资源URL不变，新配置不能确认旧停止/释放槽，恢复原实例并修正凭据/目录后可以确认。complete_if_ready只被注入从不调用，不能把其遗留实现当成当前可达漏洞；最终全域核验时决定移除遗留接口，不恢复旧重试流程。

最终七文件150个不同用例通过：torrent-contracts首跑148pass/2fail（86.13秒），两条旧测试漏带设置版本；显式先读版本后最终2/0（6.73秒）。另旧JSON入口2/0（2.89秒）；realtime/log DOM root/proxy通过。2855 collect-only，68XML按当前节点取最新1116 distinct passed、0failure/error/skip，非整库。首次核心18条有一条测试误用仅解析隐藏字段的Forms去读文本输入，改断言实际HTML后32/0，不是产品草稿丢失。039仍未冻结。

**工具profile已接通**：039回填profile_uuid/独立revision；身份不可修改，内部实际配置变化推进版本。原DB setter仅增加可选外部事务，ProfileCommands复用其验证，在同一事务校验身份/版本、保存与写固定安全回执；成功无变化也消费旧版本。不同profile独立编辑，名字/数字编号复用不接受旧身份；删除重加后旧成功回执仍返回原结果。GET归档快照同SQL读取身份/版本/内容，新PUT /api/v1/settings/archive/profiles/{name}、WS及原表单共享；冲突保留路径/超时/启停与原版本/key，显式确认才采用当前版本。浏览器广播即使已无该profile也保留原草稿节点。profile-core九条9/0（2.55秒）；profile-contracts五文件118/0（34.94秒）；加强038升级/DDL回滚/重复初始化和删除重加回执后profile-final两条2/0（0.76秒）。DOM root/proxy exit0；2863 collect-only，71 XML当前节点1193 distinct passed、0failure/error/skip，非整库。

**最新接续：稳定身份与工具安装（2026-10-16，覆盖上文待补项）**：重复失败在锁内固定existing_candidate_id/existing_work_id，删除事务外多余预查，标题与身份同SQL读取；失败保存前删除重用、原失败重放、已经显示的旧链接均不会显示新作品。成功跳转同样带稳定身份，详情打开时再核对。profile原生错误响应在已无原记录时构造仅供渲染的草稿行，不写数据库，不确认采用同名新身份。

工具安装使用ToolchainCommands：039新增独立安装revision/操作；受理与版本同事务，202固定受理、另GET结果。HTTP/WS/原表单同核心，提交协程屏蔽断线取消的commit→dispatch间隙；后台安装复用原服务与run_sync收束线程，停机先拒绝新受理并等真实执行结束。WorkerSession启动将原RUNNING标UNKNOWN；后台旧派发核对会话/状态，自动安装遇UNKNOWN也不重发。所有安装入口包括CLI通过OS文件锁互斥，替换失败恢复.previous，下次在下载前恢复中断的旧目录。普通回执只含安全状态，不回传异常内容或路径。

验证：身份五文件47/0（46.40秒），扩展数据库/详情/迁移三文件65条先63pass/2fail（73.82秒），旧迁移数38与绕过外键的测试连接已纠正，最终2/0（0.39秒）。工具安装新集成/真实安装单元31/0（2.59秒），归档/profile/迁移/worker四文件54/0（36.15秒），加强失败写回前编号复用、038升级与跨通道确认后3/0（2.47秒）；最终生命周期/手动/迁移三文件16/0（5.66秒）。真实线程/子进程锁、取消不提前释放、丢响应后继续派发、停机等待、未知重启不自动再装均有正式用例。DOM增加安装草稿/原版本/key在后续广播保留，根路径与/ehbot均通过。2875 collect-only，79XML按当前节点取最新1264 distinct passed、0failure/error/skip，非整库。

**AI总开关接续**：已复现无版本PUT仍200并覆盖值，现为第九独立设置区ai-master。SettingsCommands复用原save_ai_enabled验证/写入缓冲与短事务，HTTP/WS/原表单共用固定回执；内部实际修改也触发版本，不同功能区互不影响。缺条件428，旧版本409，冲突保留复选框/原版本/key，确认才新命令；返回普通回执，开关当前值另GET。相关四文件15/0（6.63秒），加强038升级保留旧关闭值、内部写版本及旧API显式read/save/read后最终4/0（3.42秒）。DOM复选框及原版本/key经后续广播保持，root/proxy通过。2877 collect-only，81XML当前节点1266 distinct passed，0failure/error/skip，非整库。

**AI身份/供应商当前接续（2026-10-16，覆盖上文AI待处理快照）**：039回填供应商/Key/模型稳定UUID和独立编辑revision；供应商另有keys_revision/models_revision。触发器覆盖旧内部写者，健康/用量/冷却/验证不推进编辑版本；配置变化清除原验证标记。验证写回原子核对原供应商UUID/版本/凭据版本与模型UUID/版本；Key密文读取及迟到健康写入也核对原Key/供应商，不能作用于重用数字编号的新记录。链查询/切片/序列化已同步扩展身份列。

AiProviderCommands复用提取的原纯验证与DB外部connection接缝；CAS/保存/删除/固定回执同事务。新增核对ai-providers集合版本，编辑核对单供应商版本；删除还核对Key/模型子配置版本，不连带删除其他端的新配置。ai-catalogue只用于快照前后校验，不作全局编辑锁。摘要使用私有主密钥HMAC；回执不含配置/秘密。HTTP/WS/原表单同核心；跨通道字段归一可重放，冲突/原对象删除保留原草稿，明确确认才采用当前版本与新键。保存跳转携带provider_uuid，旧链接不选择新对象。删除草稿不接受同编号的新UUID。

验证：先复现8个网络交错全部失败；identity-core86/0（20.336秒），identity-contracts四文件191/0（207.830秒）。供应商HTTP首轮3pass/3fail是隐藏字段parser误查文本name，修为隐藏字段检查加HTML值检查；后续109条106pass/3fail发现测试辅助误用于路径规则、漏补一处新增前置条件、TestClient.delete不支持json。均已修复且相关节点最终通过。补子配置删除冲突/确认后最新ai-catalogue-final五文件97/0（27.008秒）、legacy-final22/0（25.338秒）。DOM新增供应商消失草稿节点/身份/版本/key保留，root/proxy通过（64965 exit0）。compileall通过。2895 collect-only；90XML按当前完整节点去重取最新1276 distinct passed，零failure/error/skip，非单次整组/整库；不把历史1266与新用例直接相加。

**当前下一步**：Key/模型/chain/参数公开编辑命令、验证/拉取模型独立持久受理，密码库/API Key安全回执、v2能力目录与全域入口核验。供应商CRUD和add_models无网络，不恢复旧“保存必须测试通过”门禁。AiProviderCommands是供应商命令，不代表其他AI写入口已接CAS；网络仍直接调用服务，须继续持久操作和原始配置捕获/重启UNKNOWN处理。complete_if_ready遗留接口留全域核验决定清理，不恢复旧流程。P11/P12未开始；rc12保持、039未冻结，阶段未验收/暂存/提交。阶段全部验收后立即本地提交并核实SHA再进入P11。

## 已下载与路径

重下/重包/批量移除已复用 WorkCommands/ReviewBatches，任务、精确原档、计算 pin 与固定回执同事务；手工 pin 不重算，路径错误只停车一次，锁外准备后再 CAS。列表同 SQL 读取 UUID/revision；039 增路径实际变化触发版本。models.py 现在有正式字段改动，提交保留字段，仅排除既有 BOM 差异。

列表改名与详情精确归档路径复用 P8 relocation journal，两个事务核对版本，最终文件移动/路径/pin/可选重包/回执一起提交，SQL 失败恢复原文件；首次打包前只保存 pin/任务/回执。精确路径拒绝占用、不加后缀。JSON/旧表单/WS 共用命令，冲突保留名称/目录/重包选项/原版本/key；真实浏览器抽屉及已下载选择 root/proxy 通过。

downloaded-contracts 43/0（6.331秒）；downloaded-legacy 111条有5条构造重复gid触发P5唯一约束，去重场景待迁移。rename-path排除去重108/0（79.829秒）；最终rename-contracts 21/0（11.219秒），含真实进程退出/恢复/原命令重试、038升级与refile停止。新HTTP测试先重复enter已启动TestClient导致两失败，修正后余下隐藏字段parser误查文本字段的断言，最终组已通过。

固定去重预览与手动新增随后完成，以上为当前进度。collect与过滤已移除节点的累计通过数待收口，后文472/2782为历史快照。P10未验收/提交，P11/P12未开始，rc12不提升、039未冻结，TEMP保留。

前置：P9 已本地提交 f60412b，rc12/038。P10 尚未验收/提交，不冻结新增迁移。依据已批准方案第 9.5–9.7 节实施，不新增多用户、阅读器或第二调度器。

当前确认：任务命令（QueueControl）、外部账号（AccountService）、来源（SourceCommands）、摄取关联操作、认证均已有短事务版本裁决；不重写这些机制。其余入口须逐域完成，不能仅在 HTTP 请求开头检查一次版本。

| 域 / 实际入口 | 当前缺口 | 处理与验收 |
|---|---|---|
| 作品元数据：actions.patch_metadata / web candidates 编辑与锁定 | 无版本；逐字段提交，错误可留下部分修改；文件同步是后续操作 | 持久作品版本、原子字段/锁定命令及共享回执；后台抓取也推进编辑版本；ComicInfo 文件操作不能持有数据库写锁 |
| 审核 / 候选删除：candidates/batch、requeue、Web 单项和批量 | 无所见作品版本，批量重复投递结果不固定 | 每项作品身份/版本、稳定批量/子回执；审批与入队仍复用原编排，删除复用持久 removal |
| 手动新增、来源下载、打包 | 已有去重、单槽和精确产物，公开提交缺统一版本/回执 | 受理意图原子裁决，重发返回同一任务；删除重加不能匹配旧代次；网络预检不长持写锁 |
| 已下载：downloaded/batch、dedup、rename，Web 对应动作 | 缺版本/批量回执，路径操作涉及文件副作用 | 同一作品版本与发布/移动协调；复用 P8 journal，不创造新文件发布器 |
| 系统/解析/归档/路径设置 | 无版本，多项设置部分逐次写入 | 按独立保存区版本；同区原子提交和回执，不同区互不阻塞；运行时刷新在提交后 |
| 审批规则/路径规则/AI 配置 | 无配置记录版本；AI 健康/探测有高频变化 | 各独立配置版本；健康/用量不推进编辑版本；网络操作独立记录，秘密不进入普通回执 |
| 密码库/API Key | 存在一次显示秘密及加密私有存储 | 普通回执仅安全结果；生成/查看秘密不缓存明文，丢响应按明确恢复语义处理 |
| Web JSON 与旧 HTTP / WS 命令 | Web 存在独立表单编排，WS 仍收录旧入口 | Web 复用领域处理器/共享 JSON；所有旧写入口同样要求版本，缺失明确拒绝，禁止代补最新版本 |
| v2 / 能力与状态词汇 | v1 查询有 task UUID/revision，尚未统一版本目录 | v2 复用领域服务，v1 数字 ID 薄映射；公布读取位置/错误/批量合同和客户端能力 |
| 草稿 / WS | 来源/账号已保留草稿；其余表单待逐项核对 | 广播保留输入及原基础版本，409 不自动换版本重试；HTTP/WS 跨通道重发共享回执 |

执行顺序：作品/元数据 → 审核与下载/打包/已下载 → 设置/规则/AI → v2/Web/WS → 逐入口合同核验。每步先复现竞争/丢响应/回滚，再实现并定向验证；本阶段全部闭环后才验收和本地提交。进度写回本表、处理计划、PHASES、task_plan、findings 和 progress。

资源身份必须防止数据库数字 ID 删除后复用；历史回执不得作用于新对象。revision 不随进度/心跳/运行健康增长。网络和昂贵文件处理在写锁外，受理意图与最终提交分开持久化。临时报告使用具名 TEMP，正式测试位于 tests/，不创建 .scratch。

## 当前实际切片（覆盖上表中元数据尚未接通的历史缺口）

元数据编辑与主动抓取已走 WorkCommands / review.application；API、两个旧编辑表单、旧抓取表单、JS 与操作查询已接通。抓取受理及网络后提交均核对稳定身份/版本，取消和 worker 停止不发布迟到结果；同键重发不重发网络，失败/UNKNOWN 返回原公开结果。回执不附后来变化的 editor，HTTP↔WS 重放与真实 DOM 保留草稿/原版本/key 已验证。ComicInfo 长写锁、pending 同步失败饥饿留 P11。

审核已有 WorkCommands.review / inspect_review、ReviewOrchestrator.work_command 与 POST /api/v1/works/{id}/review：审批/审计/任务/打包意图/版本/回执同事务，网络准备在锁外。旧批量、旧 requeue API、旧 Web 审核仍待切换，不视为全域审核已受保护。候选列表需把行状态与身份/版本一起读取；批量父/子回执须固定结果，禁止重发后按后来状态重算 skip。

当前 2749 collect-only；13 份 JUnit 按 classname/name 取最新结果共 258 个不同用例通过，0 failure/error/skip，非整库。元数据组 66/0、旧入口回归 128/0、审核核心组 74/0、最终 work/HTTP↔WS 组 39/0，重叠不相加；metadata/realtime 真实 HTMX/Alpine DOM root/proxy 通过。原始失败与修复见 progress。阶段未验收，rc12 不提升、039 未冻结、不提前提交。

## 审核/来源下载/打包接续（覆盖上述旧审核待接项）

旧单项 Web/旧 requeue API、批量 API/Web 均已改共享命令；候选列表单个 SQL 一起读取行状态/UUID/版本。批次请求与子成功/拒绝回执持久化，同键并发或中断后不重算旧结果；拒绝与成功竞争时，同一短事务的先完成结果裁决。旧数字 ID 重用不作用于新作品。选择候选后实时刷新保留原状态/版本并提示，取消选择后后续刷新恢复；元数据草稿仍保留真实节点。

WorkCommands.enqueue / review.application.enqueue_work 共享来源下载和打包受理：回执先于 CAS/可用性/预检，预检在锁外，最终 CAS/原队列入队/回执同事务。保留 task revision 与新增 work_revision 的区别；旧任务结束后同键重发不复活，明确重做需新版本/键。convert 固定输入 UUID，原 conversion enqueue 仅提取 prepare_enqueue，内部工作流不改用公开 CAS。六个旧 Web 下载/打包入口和 JSON/WS 均已接通。

最新 2772 collect-only；26 份 XML 按节点取最新共 439 distinct passed，0 failure/error/skip，非整库。审核/038 升级新四文件 25/0；来源/打包新两文件 10/0；API/作品/入队三文件 97/0；供应商四文件 83 条先有 3 个旧前置条件失败，最终 Telegraph 12/0；打包两文件 44/0。各组重叠不相加，完整失败与修复见 progress。下一步候选删除/已下载、手动新增、配置/规则/AI/v2 与全域草稿；ComicInfo 写锁/同步饥饿仍留 P11。039 未冻结，阶段未验收，不提前提交。

## 候选删除与冲突页面接续（覆盖上述删除待接项）

WorkCommands.remove / review.application.remove_work 复用 ArchivedWorkService._begin_removal_sync；该原方法仅增加可选外部事务。稳定作品 CAS、原持久 removal/停止操作、墓碑与回执一次提交，失败全回滚；准备仍在锁外。同键回执查询先于作品存在性与目录读取。新 POST /api/v1/works/{id}/delete、旧 Web 删除及原候选批量删除均接通，HTTP↔WS 共用回执；原后台/去重内部服务入口保留，不声称已下载去重完成 P10 合同。

移除命令只返回固定受理（单项202、cleanup_status=PENDING），既有后台处理器停止生产者/清理文件；实时结果从 GET /removals/{id} 查询。批次沿用原父/子持久结果机制，成功/拒绝/中断后的重发不重算。模式参与命令摘要，删除后数字编号复用不作用于新作品。旧单项删除冲突保留原模式/身份/版本/key；批量冲突返回409并保留选择与前置条件，已离开列表项仍能原批次重发，不在错误页触发自动审核。

最后筛选/HTMX修复已核实：显式筛选释放旧选择，后台广播仍保留原状态/版本；base.html允许409/428交换错误页。此前待查的 DOM / fallback 工具会话均exit0，fallback三文件18/0。

当前2782 collect-only；30份XML按最新节点共472 distinct passed，0 failure/error/skip。新增删除领域/HTTP两文件10条；removal-core三文件17/0，removal-physical五文件30/0，removal-web三文件86/0；组间重叠不相加。真实停止回归已将原阻塞下载/迟到写屏障场景改走新命令。realtime/log DOM root/proxy通过，静态编译通过。rc12/039未冻结、阶段未验收/提交；下一步已下载管理（重下/重包/改名/去重与草稿）、手动新增及配置/v2；不改models.py既有BOM。
