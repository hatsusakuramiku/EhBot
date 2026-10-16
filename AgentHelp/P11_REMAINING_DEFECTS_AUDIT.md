# P11 Remaining Defects Audit (complete, 2026-10-16)

前置P10/R97已本地提交`f03c97717acda8906288eb9a77275d6048602f9b`；172个提交blob、空暂存区及027–038未改已核实，039冻结。首个commit因本机缺作者身份失败；仅本条命令沿用前四个阶段提交的作者重试成功，不改全局配置。P10暂存检查另发现新commands.py末尾空行，修正后通过。SHA后补留痕随本阶段交付，不另拆文档提交。

假设与边界：沿用单后台/单管理员及P8文件恢复协调，不引入第二调度器；轮转只改变检查顺序，不绕过审批/所见版本或提高预算。ComicInfo的耗时重写和全文件哈希应移出写事务，最终短事务仍须保护同作品/产物/文件身份；具体恢复接缝先核对现有Publications再定，不用简单释放锁留下可覆盖的新竞态。既有迁移不修改，必要时新增040。

1. 审批扫描改有界轮转，保留试跑最新优先 → 实际SQLite积压超过100条、不匹配/失败旧项、新增/规则修改后重访均有确定性合同。
2. 缩略图在hash协调区重新读取真实状态 → 第二请求持旧PENDING，第一请求已完成READY/FAILED后继续，实际MockTransport只请求一次；缺失文件恢复仍生效。
3. ComicInfo拆锁外准备与短提交，复用文件发布恢复 → 大CBZ重写时其他SQLite写可提交；同作品编辑/打包/移除/改名、取消和进程退出不得发布迟到内容或失去原文件。
4. 元数据待同步批次轮转并隔离失败 → 超过100条失败/未完成前缀不阻塞后项，重启/重复扫描仍可完成。

**Completion.** The four defects are implemented and verified. Version `0.3.0rc14`; the focused baseline is 131 passed, 0 failure, 0 error, 0 skipped. The phase was staged, reviewed, and committed locally as the final P11 local commit SHA verified in Git history before P12. Linux guardian execution remains a P12 check.
