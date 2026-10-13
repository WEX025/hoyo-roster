# 架构整理与定向验证

本轮固定基线：main `2dd590ef2a6f991506114c4313ca240a65b864ba`。独立新工作树开发；交付 head 的完整 SHA、草稿 PR 和开发结果评论以 PR 交付记录为准。项目代理规则仍仅以 [AGENTS.md](../AGENTS.md) 为权威，本文记录改动和实际证据。

## 行为与边界

游戏层返回 `OperationResult`，公共入口在浏览器关闭且锁释放后输出最终 PASS。成功同步输出 SAVED；保存后关闭失败分别报告 BROWSER_CLOSE、COMMIT_STATE 和 CLEANUP_PENDING，既不误报整体成功，也不把已确认保存说成未保存。候选创建、提交、取消排空和确认提交后的清理由共享流程负责，游戏采集、来源比较和 lifecycle 继续独立。

存储接收入口注入的 CandidatePolicy，取消对具体游戏模块的反向导入。SnapshotStore 明确声明提交结果和只读状态；业务层通过存储提供的候选清理操作报告待办，不再直接赋值。current、transaction、inventory 与代际恢复仍使用原有状态机。已有有效 current 经过恢复、身份和残留预检后原样复用；空初始化使用包内公开空模板，不重新手写另一份基线。

共享 JSON 原语精确读写 Decimal、统一安全路径检查。独立公共验证器不导入游戏模块，对三游戏数据精确回读并严格比较规范编码；高精度星铁数字可以离线验证、安装、inspect 和 recover，非规范末尾零及非有限数仍拒绝。运行时验证调用、超时和不可用错误统一；恢复执行发行包内的可信验证器，不执行用户代际内的代码。发行物检查要求真实非空 wheel 和 sdist，全部检查为显式失败，`python -O` 不能跳过。

星铁公开 login/sync 在选择私人路径或启动浏览器前报 GAME_PAUSED；内部保留入口也拒绝调用。格式、schema、normalize、lifecycle 和离线候选/恢复保护保留，不重新启用采集。原神字段限制和绝区零闭合条件保持，平台 I/O 与源码根核验未降级。

## 清理核对

删除前搜索了 src、tests、tools 的引用，移除：

- CallbackLoginProvider；无入口的 blocking_context/deadline/run_cleanup 链，保留 run_blocking 取消排空。
- 共享与三游戏的无消费 Redactor/短摘要 helper，保留固定错误代码与实际有消费者的业务 digest。
- 候选 `_inspection`、兼容常量、ZZZ 兼容 shutil、无 store 的旧默认候选路径；未消费且三游戏语义不同的候选 business_digest。
- 原神 SourcePass、未用圣遗物计数、未用名单 digest helper；旧 job_id、COMMIT_SHA_PRESENT=NO、DEFAULT_BROWSER_PROFILE。
- HSR 实验 CLI/采集调度和 SyncEvidence、启动检查分支与无调用者的启动矩阵；有调用者的底层浏览器权限/关闭检查继续保留在暂停实现中。
- 三游戏重复 jsonio 模块及未再使用的 simplejson 运行依赖；旧依赖许可证保留作历史归属声明。

候选使用 v4 归属标记，仅保存实际消费的游戏树和 contracts 摘要；旧/未知格式保留并拒绝清理，无兼容或迁移。CandidateScope/Policy/Workspace、current/transaction/inventory、platform_io/windows_io、来源与名单闭合检查及旧 gitignore 防泄露模式均保留。Provider 通过明确 login_page 接口提供登录页面，协调器不再反射私有 `_login_page`。

## 实际验证

macOS arm64 / Python 3.12.15。仅新临时合成身份、结果根和认证根；没有真实登录、短信、私人目录读写、CI/Actions 或审计 Agent。下面分组有重叠，不相加为唯一用例数；python 为本轮临时开发环境。最终源码以 PR head 固定 SHA 为准。

| 命令或检查 | 实际结果 |
|---|---|
| 原独审 audit_probe.py / decimal_probe.py，在固定基线的只含跟踪代码的副本上原样运行 | 已复现提前 PASS、缺 SAVED、提交异常缺状态、prepare 换代、HSR 可路由和 Decimal 精度损失；未改写原证据 |
| `python -m pytest -q tests/test_architecture_regressions.py tests/test_direct_entrypoint.py tests/test_sync_storage_rejection.py -x` | PASS：64 passed；覆盖原神/ZZZ 首次保存、无变化、关闭失败、持久化未知、清理待办、锁释放失败、离线 HSR Decimal 保存/检查/恢复、有效 prepare 不变、取消排空；此组启动后新增的 6 项发行物控制单独见下行 |
| `python -m pytest -q tests/test_architecture_regressions.py -k distribution` | PASS：16 passed，25 deselected；含真实合成 wheel/sdist 正向、缺失/空/损坏负向，普通与 -O 两模式 |
| `python -m pytest -q tests/test_login_flow.py tests/test_login_ux.py tests/test_login_contract.py tests/test_session_handoff.py tests/test_manual_captcha.py tests/test_platform_io.py tests/test_candidate_ownership.py tests/test_recovery_entrypoint.py -x` | PASS：233 passed；含登录页面接口、Context 关闭、归属与跨平台 mock |
| `python -m pytest -q tests/test_storage_faults.py -x` | PASS：17 passed；提交/刷新/清理正反控制 |
| `python -m pytest -q tests/test_recovery_atomicity.py -k 'valid_and_repeated_recovery or explicit_bad_inventory or damaged_partial or permission or bound or unowned' -x` | PASS：39 passed，10 skipped，110 deselected；skip 为没有写入或清理阶段的参数组合 |
| `python tools/platform_smoke.py` | PASS：源码、干净 wheel、干净 sdist 环境；真实 macOS 私有权限、跨进程锁、保存/恢复、身份 key 和 CLI |
| 原神/ZZZ persistent_browser，全新临时 Profile，复用临时 Chromium 二进制，仅 about:blank | PASS：真实 Chromium 启动与 Context/运行时关闭；不登录、不采集 |
| `python -m ruff check src tests tools --select F`；`python -m ruff format --check src tests tools` | PASS |
| `python tools/validate_repository.py --require-empty-baseline`；`--self-test` | PASS：公开空基线、14 个正向 fixture、81 个负向规则 |
| `python -m build --no-isolation`；`python tools/verify_distribution.py`；`python -O tools/verify_distribution.py` | PASS：实际 wheel/sdist，89 个包资源；复用已安装的锁定 hatchling 后端 |
| 两个独立临时 venv 分别 pip install 实际 wheel/sdist；从 /tmp 导入三游戏 cli/candidate/lifecycle/normalize、验证 installed contracts、精确 Decimal、CLI help | PASS：导入及 contracts 确认位于各自安装环境 |
| `python tools/check_private_data.py`；`git diff --check` | PASS：仅索引路径和公开 diff；data/ 无新增跟踪个人文件 |
| Windows 实机 SID/DACL/原生句柄/锁/替换/Chromium/本人端到端，Linux/WSL，断电持久化 | NOT_RUN：macOS mock 不替代本人 Windows 验收；参见 [平台支持](platform-support.md) |
| 真实账号登录/同步、短信、HSR 采集 | NOT_RUN；本轮未授权真实操作，HSR 继续暂停 |

开发期间出现的错误均未计为通过：受沙箱网络限制，首次依赖安装和隔离构建失败，改用获准的依赖下载及已安装后端；重构初期固定 SyncError 导出、ZZZ 关闭包装接入与合成清理故障注入随后修正。验证器正向 fixture 仍写浮点末尾零，已改用同一精确规范 writer；存储故障测试发现预检失败保留上次 COMMITTED，已把提交状态重置放回预检前，并以最后 17 项通过验证。受影响的最终分组已重跑，无未解决失败。Windows 实机保留 NOT_RUN，不宣称正式跨平台可用；本轮交付草稿 PR，暂不合并。
