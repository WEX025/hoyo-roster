# 本机使用

**崩坏：星穹铁道暂时无法获取，请不要使用。原神目前不含圣遗物和完整属性面板。** 游戏数据范围见[数据格式](data-format.md)。

## 首次使用

在 AI 客户端中请求采集本人原神或绝区零数据，默认按原神→绝区零执行。每个游戏检查同一客户端内置浏览器的登录态；需要时打开官方登录页，提示本人自行登录，并主动询问是否完成。用户确认后登录模块结束，交给当前游戏继续核验和采集。详细边界以[四模块工作流](workflow.md)和 [local-roster skill](../.agents/skills/local-roster/SKILL.md) 为准。

Codex 使用 Codex 内置浏览器；其他客户端使用自身内置浏览器。该规则与操作系统无关，不表示所有客户端或平台均已验收。客户端没有可用内置浏览器时报告能力缺失，不启动独立系统浏览器。

用户在官方页面自行处理手机号、验证码、扫码、协议和图片验证。AI 不索取这些输入，不自动发码或代提交。登录模块只等待用户确认，游戏身份和数据检查由采集模块执行。无需在登录前提供 UID。

## 会话中断与恢复

登录和游戏模块交接同一活浏览器会话及页面引用，页面在交接时保持可用。游戏间失效则只恢复当前游戏；采集中失效最多重新登录并重做当前游戏两轮采集一次，已经完成游戏保持不变。

浏览器管理登录态，不导出 Cookie/token，不把内置浏览器状态复制到 Python Profile；客户端重启后的持久化尚未验证。只退出某个游戏模块不应退出共享账号。

## 客户端桥接与离线操作

AI 依[客户端桥接](client-bridge.md)建立 host.startTask；Python worker 通过私有双向管道将登录打开、用户确认及来源读取请求交给保持活浏览器的宿主。没有宿主时返回 CLIENT_BROWSER_REQUIRED，缺少完整数据读取能力时返回 CLIENT_SOURCE_UNAVAILABLE。当前 Codex 的真实完整采集受后一项限制；不得把管道测试的合成来源当真实数据。

--client-stdio 只用于宿主连接，不在普通终端运行；源响应只在私有管道传递。--runtime-root 只选择仓库外 client-runtime-v1 的会话锁/节流元数据，不存浏览器状态。无需安装独立浏览器驱动。

Python 3.12 包可按 [README](../README.md#安装与离线维护) 安装。已有结果的 init/inspect/recover/cleanup 操作继续使用 CLI；这些操作使用明确游戏/UID 确定离线范围，不要求登录。HSR 仅保留既有离线维护和数据解释。

结果默认在已核验的本项目源码仓库 data/；无可核验源码的安装回退系统用户数据目录的 results/。--storage-root 只覆盖结果位置，不依赖 cwd，不写 site-packages。结果按游戏/UID 派生哈希隔离，显示 RESULT_SCOPE、CURRENT_POINTER 和有效时的 CURRENT_RESULT。路径与 UID 反馈时应脱敏。

POSIX 结果目录/文件使用当前用户所有权与私有权限；Windows 使用 SID/DACL 和本地固定 NTFS，不支持 reparse/junction 或云同步目录。详细边界见[平台支持](platform-support.md)。不提权、不修改用户已有目录权限来绕过失败。

本人旧认证和结果目录保留原地，不读取、复制、迁移或删除。退出客户端账号须在确切会话的官方页面操作，不能通过删除本机目录代替。

## 检查与恢复

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
hoyo-roster --game genshin --uid YOUR_UID --operation recover
```

inspect 在 current 缺失或失效时仍给出 `EMPTY` / `INVALID`、代际 UUID 与 `VERIFIED` / `UNVERIFIED`、是否有恢复日志和候选状态；不输出 UID 或 JSON 内容；会显示本机结果路径，公开反馈前应脱敏。UUID 是本机恢复标识，也应在公开反馈时脱敏。

保存有三个边界：

- `NOT_COMMITTED`：新 current 未提交。之前的数据仍有效，已写恢复日志时必须 recover 后再运行。
- `COMMITTED`：新 current 已替换且完成该平台提供的持久化操作（POSIX 文件及父目录 fsync；Windows 文件刷新与 WRITE_THROUGH 替换）。Windows 不承诺断电持久化等价，见平台边界。旧代际或候选清理失败仅是清理待办，已经保存的数据有效；不会把保存说成失败。
- `DURABILITY_UNKNOWN` / `COMMIT_DURABILITY_UNKNOWN`：替换已发生，其后的平台持久化检查失败。新旧代际和恢复日志都保留，不声称完成保存，停止采集并检查恢复。

recover 在锁内先完整预检，再提交指针，最后清理。身份权威是独立于 current 的 `workspace-identity.json`，绑定所选游戏、UID 摘要和区服；首次离线空基线尚未绑定区服，首次完整验证的业务提交会在指针提交前持久绑定，回退空基线不解除绑定。缺失、损坏或矛盾的身份边界拒绝恢复和清理，不能从损坏 current 推断为任意区服。

预检核验目标所有权、身份、文件权限、完整数据与验证器，核验日志的两个不同引用、代际身份、完整 inventory 摘要、目标的完整清单及待清理残留。全部通过后才替换/确认指针持久化，再清理日志指明的非当前代际。预检拒绝不会改变 current，也不会删除候选或代际。恢复失败同样报告 `COMMIT_STATE`；replace 前失败为 NOT_COMMITTED，replace 后父 fsync 失败为 DURABILITY_UNKNOWN，保留所有可能引用的代际和日志。已经提交后的清理、日志 unlink 或清理 fsync 失败报告 COMMITTED 与 CLEANUP_PENDING，重复 recover 可继续核验。若清理仍失败，继续报告待办，不删除 current。日志或代际来源不明时停止，不能因为目录名是 UUID 就删除。

current 失效时，先 inspect，选取本人确认且显示 VERIFIED 的代际：

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation recover --generation VERIFIED_GENERATION_UUID
```

显式恢复会重新完整验证目标并切换指针；`UNVERIFIED`、别的身份或任意 UUID 都被拒绝。未知/损坏残留保留给人工核验，不提供强制忽略验证的删除开关。

清理中断而只剩部分旧代际时，recover 依据提交前已完整验证并持久化的文件清单，核对现存路径/哈希/所有权；出现新文件或改动就拒绝。恢复日志缺失时，先保留 current，用 inspect 选择完整 VERIFIED 的非当前代际，执行 `--operation cleanup-generation --generation VERIFIED_GENERATION_UUID`；当前代际和无法证明归属的残留不能删。

候选残留与代际恢复分别处理。只有程序能证明归属的候选才允许清理。普通同步在候选预检/存储提交失败或持久化未知时保留已生成候选，供 inspect 后核验；确认 COMMITTED 后才自动清理。新候选在复制 contracts 或写入业务数据前，先原子落盘 v4 归属标记，绑定项目、游戏、UID 摘要、区服和 run UUID；building 与 validated 状态都保留同一范围。inspect/cleanup 要求显式所选身份，核对标记、权限和已写 Profile 的身份；validated 候选还核对实际游戏树及 contracts 摘要。已持久绑定区服时也要求候选区服一致，即使 current 缺失或损坏也不降低约束；空基线身份边界尚未绑定区服时要求候选所记区服属于该游戏支持范围并与其 Profile 一致。候选操作也核验留存完整代际的身份；矛盾或部分清理残留先处理恢复。身份边界无法证明时 inspect 显示 IDENTITY_BOUNDARY=UNPROVEN，候选不允许清理。

`SCOPE_MISMATCH` 表示候选与所选身份/区服或其实际 Profile 冲突；`UNOWNED` 表示标记缺失、旧格式、不完整、权限或内容验证失败。两者均 `CANDIDATE_CLEANUP_ALLOWED=NO`，cleanup 报 `CANDIDATE_SCOPE_MISMATCH` 或 `CANDIDATE_OWNERSHIP_UNPROVEN` 并保留数据。`--run-id` / `--generation` 缺失、非规范 UUID、用在不对应操作中均给固定错误代码；错误值不回显。

创建目录后、初始标记持久化前若中断，即使只剩空 UUID 目录或 `.candidate-state.json.*.tmp`，也属于 UNOWNED；文件名、目录位置不是授权依据。标记成功后中断的 building 候选，只有通过上述归属核验才可清理。候选清理中断若丢失标记或内容变得不可验证，同样保留，不自动补造归属。没有旧格式兼容不意味着允许删除旧格式数据。

遇到拒绝清理：先停止采集任务，保留共享客户端浏览器及 current、transaction 和所有可能被引用的代际；私下备份该候选，再核对本人创建记录、游戏/UID/区服和文件内容。无法确定来源就保留，不能为了继续同步删掉、改写标记或移动有效证据，也不要把个人文件上传到公开反馈。只有本人独立确认归属、完成备份后才自行处置确切候选范围，不删共享父目录。

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
```

确认显示 BUILDING/VALIDATED 且 `CANDIDATE_CLEANUP_ALLOWED=YES` 后：

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation cleanup-candidate --run-id OWNED_CANDIDATE_UUID
```

## 卸载

python -m pip uninstall hoyo-roster 只卸载 Python 包，不清理正式结果、客户端浏览器登录态或旧认证目录。删除源码克隆前应核对并私下备份其中的 data/。清理必须核验确切范围，见[隐私说明](privacy.md)。

## 固定版本验收预检

关闭工具后更新并重新安装选定代码。验收时用该环境的 Python 运行 `python tools/verify_installation.py`，不要设置 `PYTHONPATH=src` 来掩盖安装差异。只有 `source_worktree_clean` 和 `installation_matches_source` 都为 true，才将正式 CLI 结果归于所报 source_sha；版本号相同也可能安装了旧代码。该检查只读取源码/安装包及 Git 状态，不读取认证内容。
