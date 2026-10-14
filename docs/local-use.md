# 本机使用

**崩坏：星穹铁道暂时无法获取，请不要使用。原神目前不含圣遗物和完整属性面板。** 本文操作示例仅用于当前可用的原神（`genshin`）和绝区零（`zzz`）；保留的 `hsr` 参数、源码或历史验证不是使用建议。绝区零已支持能力见[数据格式和完整度](data-format.md)；原神角色名单仍须闭合，`partial` 表示覆盖字段有限。该停用说明是当前产品决定，不将未完成的真实验收推断为上游原因。

## 首次使用

所有 OS 首次使用都直接运行 `hoyo-roster`：先在各游戏官方页面由本人登录，再自动发现该账号所有受支持国服 UID，**不先询问或要求填写 UID**。省略游戏等同 `--game all`，原神/绝区零也可分别选择；星铁不登录、不采集。默认 sync/login/discover 都无需 UID；init/inspect/recover 等指定身份的离线维护仍需要明确游戏/UID。`--operation discover` 不采集角色；本人已有明确身份时可选 `--uid`，维持精确身份流程。

首次发现用认证根内新 UUID 范围创建私有 Profile，浏览器确认关闭后才把本次新 Profile 放到首个发现身份的空目标中；已有目标不覆盖、不复制认证材料。其他角色使用各自独立会话，必要时本人另行登录。认证根 onboarding/selected-游戏.json 保存本机 UID/区服选择，私有权限保护、不在终端打印 UID，不证明登录或所有权；后续命令仍从官方 Binding 精确核验后才采集。默认命令复用这些选择；显式 discover 重新发现当前本人登录账号。失败/关闭不确定保留本次临时范围，不探索或清理旧会话。

按 README 安装 Python 3.12 和项目；浏览器选择及 Chromium 备用安装见下节。源码运行及从本地克隆执行 `pip install .` / `pip install -e .` 后，结果默认在已核验的本项目仓库 `data/`。定位依据是代码所在源码或安装元数据中的本地源码地址，不依赖 cwd；从仓库子目录或其他目录执行也不会把结果写到其他仓库。直接安装 wheel、源码目录已删除或无法核验时，回退到 platformdirs 系统用户数据根的 `results/`，不会静默写入 `site-packages`。

`--storage-root /absolute/private/results` **只选择结果根**，不改变浏览器会话位置；相对路径被拒绝。仓库内覆盖只能位于已核验的本项目 `data/` 下。`--auth-root /absolute/private/auth` 单独选择认证根，默认使用 platformdirs 系统用户数据根（各平台位置见 [README](../README.md#输出与隐私)）；必须在所有 Git 仓库和包源码之外并通过当前用户的私有权限检查。Profile、认证 key/指纹、节流与认证锁按游戏/UID 隔离；不同结果根共用同一认证范围时仍由认证锁互斥。

结果范围为 `<结果根>/users/<scope>/<game>/<hash>/`；认证元数据范围为 `<认证根>/users/<scope>/<game>/<hash>/`。浏览器 Profile 单独位于较浅的 `<认证根>/profiles/<身份哈希>/browser-profile/`，仍按 user/game/UID 唯一隔离，由同一身份锁保护；不会迁移或复用旧布局的 Profile。scope/hash 由身份确定，不含明文 UID。Windows 文件系统内部使用受检的长路径命名空间，而传给 Chromium 的 user-data-dir 使用普通盘符路径；认证根应尽量短，为浏览器内部数据库留出路径空间。程序显示 `RESULT_SCOPE` 与 `CURRENT_POINTER`；存在有效当前结果时显示 `CURRENT_RESULT`，即 `<结果范围>/<当前代际>/data/<游戏名称>/`，含 `profile.json`、`roster.json`、`builds/` 和绝区零 `bangboos.json`。`current.json` 指向当前代际，保留既有名单/build/bangboo 快照、无变化时间戳和原子保存语义。

POSIX 上，程序逐层把**新建应用目录**设为 0700，不修改已有共享父目录。clone 创建且包含 `.gitkeep` 的顶层 `data/` 可保持属于本人、不可被组/其他人写入的 0755 权限；其下身份目录仍须 0700。其他已存在的结果根、认证根及身份目录必须属于当前用户且为 0700，否则报 `UNSAFE_PRIVATE_DIRECTORY`。普通结果和元数据为 0600，浏览器内部文件权限由 Chromium 管理并被 0700 Profile 包围。

Windows 使用当前进程用户 SID 与 DACL 校验所有权和私有权限，不把 chmod/0600 当成 Windows 安全证据。新目录和文件创建时就给当前 SID 全权限并阻断其他 SID 的继承访问；Profile 根同样受保护。已有不安全根只拒绝，不修复其 ACL。clone 的公共 `data/` 容器只允许当前用户、SYSTEM、Administrators 写入，身份子目录仍须私有。路径各层的 reparse/junction 均拒绝。普通用户不需提权；详细边界见 [平台支持](platform-support.md)。

```bash
hoyo-roster --game all --operation login
hoyo-roster
```

`init` 只在没有 current 时建立验证后的空工作区；已有有效 current 经预检后原样复用，不换代、不改文件或时间戳，不联网。`login` 检查精确官方 Binding，不采集角色。默认 `sync` 检查会话、核验身份、采集第1次和第2次、验证、保存或报告无变化；无变化保留业务时间戳。相同游戏/UID 始终选择同一隔离范围；不同游戏/UID 使用独立 Profile、锁和节流状态。

启动浏览器后，程序先在同一持久 Context 中打开受限的临时官方页面，等待页面资源与已有会话校验完成，再发送初始 Binding；初始化页允许官方资源和已注册的会话校验，不允许短信、二维码创建或游戏业务请求。临时页关闭后才继续核验；初始化超时/网络/操控错误单独停止，不当成认证过期。初始化或会话指纹本身都不证明身份；每次登录/同步仍须通过精确官方 game/UID/region Binding。初始化阶段遇风险、限流或无效校验响应同样停止。程序不导出 Cookie/token，也不延长官方有效期。关闭持久 Context 完成后才停止 Playwright；关闭失败仍尝试停止运行时，不报告登录成功。

交互终端在确定需要新登录时，先一次说明二维码和手机号两种方式、本人配合步骤、与官方通信、本机 Profile 保存及复用范围、协议和权限边界；再通过一次“允许并选择二维码 / 手机号 / 取消”询问取得授权。显式 `--login-method` 仅选择方式，交互终端仍询问允许；`--allow-login` 可以预先允许说明中的操作。手机号、验证码、扫码、必要协议的阅读与勾选和最终确认都由本人在官方页面完成。程序不自动勾选、不发送短信、不扩大权限。未知新协议、额外权限或风险提示不属于这次允许，须暂停并由本人另行确认。有效会话直接复用，不再次要求登录授权；错误账号/游戏/区服立即停止，不自动切换账号。

非交互进程需登录时使用 `--non-interactive --login-method qr|phone --allow-login`；缺失方式报 `AUTHENTICATION_REQUIRED`，缺失允许报 `LOGIN_CONSENT_REQUIRED`，都在打开可执行认证的登录页面前停止。`--login-method cancel` 取消新登录。自动化调用者应先向本人展示本段说明并取得允许，再传入该标志；它不替代官方页面协议确认。

```bash
hoyo-roster --game genshin --non-interactive --login-method qr --allow-login
```

通常直接使用正常同步命令：需登录时在同一浏览器完成登录，随后继续两次采集、验证和保存；不必先跑临时登录脚本。程序在确认需要本人参与且已取得允许后立即输出 `WAITING_FOR_USER`，再打开页面。已识别的官方普通面板/方式控件唯一且可见时自动点击一次：原神头像、米游社“登录”、SDK 模式 SVG 和“短信登录”标签。操作不包含退出账号、切换角色、协议、发码或 CAPTCHA；结构未知或短暂操控故障在入口等待约 15 秒后显示 `LOGIN_PANEL_MANUAL`，请本人在同一窗口打开/切换面板。点击结果不确定时不重复点击。

`--login-timeout 900` 是默认本人登录总等待预算，范围 0 < 秒数 ≤ 3600；页面打开、入口等待、CAPTCHA 和身份轮询共用期限，不逐阶段重新计时。此前已有会话的初始化、页面导航和前台控制各最多 30 秒（用户设置更短时取更短）。等待显示 `WAITING_FOR_ENTRY`；短暂操控超时显示 `CONTROL_RETRY`，在期限内等待恢复。一直未见入口报 `LOGIN_TIMEOUT`；持续操控超时、页面关闭或操控不可用报 `LOGIN_CONTROL_UNAVAILABLE`。`--headless` 只复用精确已验证会话，需新登录时停止。Ctrl-C 随时取消，排空后台回执任务、释放监听和锁并关闭本次浏览器；正在进行的本机写入先结束再释放锁。

`QR_READY` 表示 SDK 二维码图片已加载且未显示过期；`QR_SCANNED_WAIT_CONFIRM` 只表示页面提示扫码，仍需本人在手机确认。入口等待同时检查精确 Binding；扫码成功后面板消失或跳转不再因找不到旧入口而判失败。`LOGIN_CONFIRMED` 仅说明身份通过，公共最终 PASS 仍须等完整操作、清理和浏览器关闭结束；关闭失败不会报最终成功。

手机号方式显示 `WAITING_FOR_PHONE_AND_TERMS`，请本人在官方窗口输入手机号、阅读并确认协议、点击一次发送并输入验证码。程序不读取输入值或自动发送。只有观察到实际官方 POST 才输出 `SMS_REQUESTING`；回执须为 HTTP 200、整数 retcode=0、布尔 `data.sent_new` 和非负整数 `data.countdown`。`SMS_ACCEPTED` 表示官方接受新发码，不证明手机送达；`SMS_COOLDOWN` 表示已有验证码/倒计时，未新发短信。点击、倒计时或单独 retcode=0 都不能证明发码。网络失败、畸形回执或请求后 30 秒仍无回执报 `SMS_RECEIPT_UNKNOWN`，读取单次回执最多 5 秒；不重试。官方拒绝或同一命令又观察到发码请求报 `SMS_REQUEST_REJECTED`，风险/限流保留原错误码。手动 CAPTCHA 期间不推进身份探测，清除后再检查已观察的回执。回执未知时停止本次流程并做既有关闭/清理，不以再次点击试探；固定状态不能替代手机送达确认。

图片 CAPTCHA 显示 `WAITING_FOR_MANUAL_CAPTCHA`，请本人在本工具打开的对应官方浏览器窗口点击图片或操作滑块：原神为 `act.mihoyo.com/ys/event/calculator/`，绝区零为 `www.miyoushe.com/zzz/`。星铁暂时无法获取，请不要使用其保留的登录/采集接口。AI 不自动选择、不解图、不绕过验证。程序只读取可见验证面板或图片/滑块操作提示，不读取图片、答案或输入值；等待期间保留同一登录页面、Context 和 Profile，暂停自动 Binding 探测，不自动发码或重复发送短信。本人操作官方页面仍可继续。

面板消失显示一次 `CAPTCHA_CLEARED`，只恢复原有等待/核验流程；本人继续完成发码、输入验证码及确认，精确 Binding 通过才算登录成功。面板取消/消失、短信倒计时均不能证明验证通过或短信实际送达，程序不自动重发。持续显示期间提示不随轮询重复，重新出现则提示新一轮等待；操控短暂超时在原阶段期限内恢复。图片验证等待计入本人登录总预算 `--login-timeout`，到期报 `LOGIN_TIMEOUT`，持续操控故障或窗口关闭报 `LOGIN_CONTROL_UNAVAILABLE`；Ctrl-C 取消并做既有清理，Profile 不删除。未知验证 UI 可能无法被识别；官方 Binding 风控/限流、未知协议和额外权限仍按原规则停止，不能为通过验收扩大请求白名单。

## 浏览器选择和会话复用

新会话按系统自动选择：Windows 优先已安装的 Edge（`msedge`），macOS 优先已安装的 Chrome（`chrome`），Linux 使用 Playwright Chromium（`chromium`）。不跟随默认浏览器设置，不支持系统 Safari。首选浏览器缺失或首次启动失败时，在接触官方页面前尝试 Chromium；未安装它会报 `BROWSER_UNAVAILABLE`，请在同一虚拟环境运行 `python -m playwright install chromium`。程序不自动下载，不安装或覆盖系统 Edge/Chrome。

每个 `browser-profile/` 是私有会话容器，其中 `browser-choice.json` 保存版本和选定 channel，实际浏览器目录为 `browser-profile/<channel>/`。首次启动的失败目录和备用目录分离，不共享浏览器数据库或复制登录材料。启动前落盘选择；取消、启动结果不确定或持久化失败时保留目录并停止。选定后不随系统浏览器安装/默认设置变化而换浏览器，可见与无界面模式都使用同一个完整 channel。终端输出 `BROWSER_CHANNEL`，首次回退输出 `BROWSER_FALLBACK=CHROMIUM`。

已有会话的选定浏览器不可用时停止，请恢复该浏览器后重试；登录失效、验证码、身份不匹配、风控和限流不触发备用浏览器。日常浏览器的 Profile 和登录态不复用。当前会话布局不迁移旧 Profile：无选择标记的非空目录、标记损坏或选定目录缺失报 `BROWSER_PROFILE_INVALID`，保留原目录。需要新会话时显式选择新的仓库外 `--auth-root`，由本人首次登录；不会为了启用系统浏览器探索、覆盖或清理旧认证根。

## 升级

关闭本工具后更新或重新安装代码，再运行原命令。本项目尚未发布，本轮不兼容历史目录/候选/恢复格式，不提供迁移，不搜索、读取或清理旧默认路径。默认认证目录策略已统一为平台系统用户数据根；已有私人结果和 Profile 留在原处。

选定范围里若存在未知或旧格式残留，程序保留并拒绝继续操作；不能删除身份标记来重建或解除区服约束。本次代码开发不操作任何真实用户范围。

同步/登录的最终 `STATUS=PASS` 只在浏览器和运行时关闭、工作区锁释放后输出。`PROGRESS=SAVED` 单独表示已经保存；随后关闭失败会报 `BROWSER_CLOSE=FAILED` / `BROWSER_CLOSE_FAILED`，并同时给出 `COMMIT_STATE` 和 `CLEANUP_PENDING=YES|NO`，不会再输出 PASS。确认已保存与完整操作成功是不同事实；以这些独立状态决定检查或恢复，不重复采集来掩盖关闭错误。

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

recover 在锁内先完整预检，再提交指针，最后清理。身份权威是独立于 current 的 `workspace-identity.json`，绑定所选游戏、UID 摘要和区服；首次离线空基线尚未绑定区服，首次完整验证的业务提交会在指针提交前持久绑定，回退空基线或 logout 不解除绑定。缺失、损坏或矛盾的身份边界拒绝恢复和清理，不能从损坏 current 推断为任意区服。

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

遇到拒绝清理：关闭本工具和确切浏览器，保留 current、transaction 和所有可能被引用的代际；私下备份该候选，再核对本人创建记录、游戏/UID/区服和文件内容。无法确定来源就保留，不能为了继续同步删掉、改写标记或移动有效证据，也不要把个人文件上传到公开反馈。只有本人独立确认归属、完成备份后才自行处置确切候选范围，不删共享父目录。

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
```

确认显示 BUILDING/VALIDATED 且 `CANDIDATE_CLEANUP_ALLOWED=YES` 后：

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation cleanup-candidate --run-id OWNED_CANDIDATE_UUID
```

## 常见停止和建议

| 状态 | 下一步 |
|---|---|
| `GAME_PAUSED`（星铁 login/sync） | 入口在选择私人路径或启动浏览器前拒绝；仅保留离线 init/inspect/recover 等数据操作 |
| 原神 `partial` | 目前不含圣遗物和完整属性面板；保存时名单仍须闭合，不补造缺失字段 |
| `AUTHENTICATION_REQUIRED` / `LOGIN_TIMEOUT` | 本人检查官方登录状态，明确选择方式后重新运行 |
| `LOGIN_CANCELLED` / `INTERRUPTED` | 本人准备好后重新运行 |
| `LOGIN_CONSENT_REQUIRED` | 阅读登录前说明并取得本人允许，再显式传入 `--allow-login` |
| `LOGIN_CONTROL_UNAVAILABLE` | 检查浏览器/操控连接后重新运行；不表示官方不支持该登录方式 |
| `LOGIN_NETWORK_ERROR` / `LOGIN_SESSION_INITIALIZATION_FAILED` | 检查官方页面加载/会话校验；保留 Profile，不导出凭据 |
| `WRONG_ACCOUNT` / Binding 错误 | 确认本人游戏、UID、区服；不要借其他会话绕过 |
| `RATE_LIMITED` / `RISK_CONTROL` | 停止本轮，按官方提示处理；不自动重试或代解验证 |
| 双次漂移 / schema / lifecycle 错误 | 旧 current 保留，等待来源稳定或提交脱敏问题反馈 |
| `USER_WORKSPACE_BUSY` | 等另一同身份进程退出，不删锁文件来绕过 |
| Profile 在用或 Singleton 残留 | 关闭确切本工具浏览器并检查；不删除正在使用的 Profile |
| `CANDIDATE_SCOPE_MISMATCH` / `CANDIDATE_OWNERSHIP_UNPROVEN` | 保留并私下备份候选，核实来源后由本人处置；不按 UUID 或临时文件名删除 |
| `GENERATION_REQUIRED` / `INVALID_GENERATION` / `INVALID_CANDIDATE_RUN_ID` | 检查 inspect 标识及 --help，不复用其他身份的 UUID |
| `BROWSER_CLOSE_FAILED` | 检查本工具浏览器是否已关闭；同时核对 COMMIT_STATE 与 CLEANUP_PENDING，保存成功仍保留 |
| `WORKSPACE_VALIDATOR_TIMEOUT` / `VALIDATOR_TIMEOUT` | 本机离线验证超时，保留 current 和恢复证据，检查环境后重试 |
| `RECOVERY_REQUIRED` / 持久化未知 / residue | inspect 后按上节恢复；不随意删 UUID 目录 |
| Chromium 缺失 | 在相同虚拟环境运行 `python -m playwright install chromium` |
| `BROWSER_UNAVAILABLE` | 新会话的 Chromium 备用缺失时按上一行安装；已有选择时恢复所选 Edge/Chrome/Chromium，不自动切换 |
| `BROWSER_PROFILE_INVALID` | 保留原目录，核对所选认证根；旧布局不迁移，新会话显式使用新的仓库外 `--auth-root` |

## 退出登录与卸载

浏览器关闭后执行 `--operation logout`：只删除该身份 Profile 和已验证身份指纹；角色数据保留。发现 Singleton 标识时停止，要求先检查是否仍有浏览器进程。退出登录不会退出其他应用的米游社会话。

卸载代码：`python -m pip uninstall hoyo-roster`；这不删除任何个人结果或 Playwright 浏览器。需要彻底清理时，关闭所有本工具进程，按 privacy 文档核对范围后自行删除选定身份范围；保留数据或先做私有备份。不要把整个共享父目录作为清理目标。

可见登录与 `--headless` 会话复用使用同一个已选定的完整浏览器 channel，避免两个浏览器混用同一 Profile。历史 Mac Chromium 合成交接证据见 [Profile 验证记录](browser-profile-validation.md)，系统浏览器选择验证见 [浏览器选择验证](browser-selection-validation.md)；它不延长官方 Cookie 期限，也不自动恢复已失效认证。

本轮登录流程的公开静态证据、合成回归及真实验收边界见 [登录流程验证记录](login-workflow-validation.md)。
