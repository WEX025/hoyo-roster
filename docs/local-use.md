# 本机使用

**崩坏：星穹铁道暂时无法获取，请不要使用。原神目前不含圣遗物和完整属性面板。** 本文操作示例仅用于当前可用的原神（`genshin`）和绝区零（`zzz`）；保留的 `hsr` 参数、源码或历史验证不是使用建议。绝区零已支持能力见[数据格式和完整度](data-format.md)；原神角色名单仍须闭合，`partial` 表示覆盖字段有限。该停用说明是当前产品决定，不将未完成的真实验收推断为上游原因。

## 首次使用

所有 OS 都直接运行 `hoyo-roster`，默认依次执行原神→绝区零；各游戏入口检查同一账号会话，必要时暂停并登录，之后继续当前游戏。无需先提供 UID；官方 Binding 自动发现本人受支持身份，每个角色仍须精确核验游戏、UID 和区服。显式 `--game` 仅执行该游戏，`--uid` 可进一步精确选择已有身份。崩铁暂停，不登录、不采集。

登录模块拥有一个账号级持久 Profile/Context；各游戏在同一上下文创建并关闭自己的页面和客户端，账号锁保持到真正关闭上下文。`--operation login` 和 `discover` 只处理认证及身份，不创建业务 current。多角色不另建 Profile 或要求独立登录。游戏间会话失效时，仅恢复当前游戏；采集中失效最多恢复一次，重做该游戏两轮采集，已完成游戏保持不变。普通单游戏失败继续其他游戏并汇总失败；身份冲突、风控、共享认证失败停止账号任务。

按 README 安装 Python 3.12 和项目；浏览器选择及 Chromium 备用安装见下节。源码运行及从本地克隆执行 `pip install .` / `pip install -e .` 后，结果默认在已核验的本项目仓库 `data/`。定位依据是代码所在源码或安装元数据中的本地源码地址，不依赖 cwd；从仓库子目录或其他目录执行也不会把结果写到其他仓库。直接安装 wheel、源码目录已删除或无法核验时，回退到 platformdirs 系统用户数据根的 `results/`，不会静默写入 `site-packages`。

`--storage-root /absolute/private/results` **只选择结果根**，不改变浏览器会话位置；相对路径被拒绝。仓库内覆盖只能位于已核验的本项目 `data/` 下。`--auth-root /absolute/private/auth` 单独选择认证根，默认使用 platformdirs 系统用户数据根（各平台位置见 [README](../README.md#输出与隐私)）；必须在所有 Git 仓库和包源码之外并通过当前用户的私有权限检查。Profile、认证 key/指纹、节流与认证锁按游戏/UID 隔离；不同结果根共用同一认证范围时仍由认证锁互斥。

结果范围为 `<结果根>/game-results-v3/<scope>/<game>/<hash>/`；认证范围为 `<认证根>/account-session-v1/`，共享 Profile 位于其 `browser-profile/`，账号锁保护整个上下文生命周期。scope/hash 由身份确定，不含明文 UID。Windows 文件系统内部使用受检的长路径命名空间，而传给 Chromium 的 user-data-dir 使用普通盘符路径；认证根应尽量短，为浏览器内部数据库留出路径空间。程序显示 `RESULT_SCOPE` 与 `CURRENT_POINTER`；存在有效当前结果时显示 `CURRENT_RESULT`，即 `<结果范围>/<当前代际>/data/<游戏名称>/`，含 `profile.json`、`roster.json`、`builds/` 和绝区零 `bangboos.json`。`current.json` 指向当前代际，保留既有名单/build/bangboo 快照、无变化时间戳和原子保存语义。

POSIX 上，程序逐层把**新建应用目录**设为 0700，不修改已有共享父目录。clone 创建且包含 `.gitkeep` 的顶层 `data/` 可保持属于本人、不可被组/其他人写入的 0755 权限；其下身份目录仍须 0700。其他已存在的结果根、认证根及身份目录必须属于当前用户且为 0700，否则报 `UNSAFE_PRIVATE_DIRECTORY`。普通结果和元数据为 0600，浏览器内部文件权限由 Chromium 管理并被 0700 Profile 包围。

Windows 使用当前进程用户 SID 与 DACL 校验所有权和私有权限，不把 chmod/0600 当成 Windows 安全证据。新目录和文件创建时就给当前 SID 全权限并阻断其他 SID 的继承访问；Profile 根同样受保护。已有不安全根只拒绝，不修复其 ACL。clone 的公共 `data/` 容器只允许当前用户、SYSTEM、Administrators 写入，身份子目录仍须私有。路径各层的 reparse/junction 均拒绝。普通用户不需提权；详细边界见 [平台支持](platform-support.md)。

```bash
hoyo-roster --game all --operation login
hoyo-roster
```

`init` 只在没有 current 时建立验证后的空工作区；已有有效 current 经预检后原样复用，不换代、不改文件或时间戳，不联网。`login` 检查精确官方 Binding，不采集角色。默认 `sync` 检查会话、核验身份、采集第1次和第2次、验证、保存或报告无变化；无变化保留业务时间戳。相同游戏/UID 始终选择同一隔离范围；不同游戏/UID 使用独立结果范围及结果锁，登录态、账号锁和请求节流由账号共享。

启动浏览器后，程序先在同一持久 Context 中打开受限的临时官方页面，等待页面资源与已有会话校验完成，再发送初始 Binding；初始化页允许官方资源和已注册的会话校验，不允许短信、二维码创建或游戏业务请求。临时页关闭后才继续核验；初始化超时/网络/操控错误单独停止，不当成认证过期。初始化或会话指纹本身都不证明身份；每次登录/同步仍须通过精确官方 game/UID/region Binding。初始化阶段遇风险、限流或无效校验响应同样停止。程序不导出 Cookie/token，也不延长官方有效期。关闭持久 Context 完成后才停止 Playwright；关闭失败仍尝试停止运行时，不报告登录成功。

交互终端在确定需要新登录时，先一次说明二维码和手机号两种方式、本人配合步骤、与官方通信、本机 Profile 保存及复用范围、协议和权限边界；再通过一次“允许并选择二维码 / 手机号 / 取消”询问取得授权。显式 `--login-method` 仅选择方式，交互终端仍询问允许；`--allow-login` 可以预先允许说明中的操作。手机号交互选择支持临时隐藏输入并自动填写、获取一次验证码；辅助流程在确认新发码回执后主动询问验证码，以隐藏输入临时代填同一官方页并提交一次；扫码、图片 CAPTCHA 和协议阅读仍本人操作。程序单独询问是否允许代勾已识别协议，未获明确允许时等待本人勾选；交互选择手机号包含临时填写和一次发码授权，非交互参数见下文。未知新协议、额外权限或风险提示不属于这次允许，须暂停并由本人另行确认。有效会话直接复用，不再次要求登录授权；错误账号/游戏/区服立即停止，不自动切换账号。

非交互进程需登录时使用 `--non-interactive --login-method qr|phone --allow-login`；缺失方式报 `AUTHENTICATION_REQUIRED`，缺失允许报 `LOGIN_CONSENT_REQUIRED`，都在打开可执行认证的登录页面前停止。`--login-method cancel` 取消新登录。自动化调用者应先向本人展示本段说明并取得允许，再传入该标志；它不替代官方页面协议确认。

```bash
hoyo-roster --game genshin --non-interactive --login-method qr --allow-login
```

通常直接使用正常同步命令：需登录时在同一浏览器完成登录，随后继续两次采集、验证和保存；不必先跑临时登录脚本。程序在确认需要本人参与且已取得允许后立即输出 `WAITING_FOR_USER`，再打开页面。已识别的官方普通面板/方式控件唯一且可见时自动点击一次：原神头像、米游社“登录”、SDK 模式 SVG 和“短信登录”标签。操作不包含退出账号、切换角色、协议、发码或 CAPTCHA；结构未知或短暂操控故障在入口等待约 15 秒后显示 `LOGIN_PANEL_MANUAL`，请本人在同一窗口打开/切换面板。点击结果不确定时不重复点击。发出人工接管提示后，程序停止普通面板/方式和待执行的手机号自动动作，只保留同一窗口的人工等待、验证码观察及精确身份核验；迟到控件不会重新触发自动点击。

`--login-timeout 900` 是默认本人登录总等待预算，范围 0 < 秒数 ≤ 3600；页面打开、入口等待、CAPTCHA 和身份轮询共用期限，不逐阶段重新计时。此前已有会话的初始化、节流等待和初次 Binding 共用最多 30 秒预算（用户设置更短时取更短）。浏览器单次启动、业务 HTTP 请求/JSON 读取及业务页面导航各最多 30 秒，原神登记列表响应观察最多 45 秒。持久节流时间超过 30 秒时报 REQUEST_SCHEDULE_INVALID，保留状态；检查系统时钟，不删除或清零节流。本人方式选择在登录预算之前，可 Ctrl-C 取消。等待显示 `WAITING_FOR_ENTRY`；短暂操控超时显示 `CONTROL_RETRY`，在期限内等待恢复。一直未见入口报 `LOGIN_TIMEOUT`；持续操控超时、页面关闭或操控不可用报 `LOGIN_CONTROL_UNAVAILABLE`。`--headless` 只复用精确已验证会话，需新登录时停止。Ctrl-C 随时取消，终端选择/隐藏号码输入不会阻塞进程退出；恢复终端回显，排空后台回执任务、释放监听和锁并关闭本次浏览器。CLI 负责浏览器 SIGINT 后的关闭，重复任务取消也等待关闭完成；真实关闭失败仍报 BROWSER_CLOSE_FAILED。关闭和文件系统写入排空不强杀或伪造截止成功，正在进行的本机写入先结束再释放锁；若它们持续未完成，保留该运行并报告 pending，不并行启动另一会话。

`QR_READY` 表示 SDK 二维码图片已加载且未显示过期；`QR_SCANNED_WAIT_CONFIRM` 只表示页面提示扫码，仍需本人在手机确认。入口等待同时检查精确 Binding；扫码成功后面板消失或跳转不再因找不到旧入口而判失败。`LOGIN_CONFIRMED` 仅说明身份通过，公共最终 PASS 仍须等完整操作、清理和浏览器关闭结束；关闭失败不会报最终成功。

非交互且未开启自动填写时，手机号显示 `WAITING_FOR_PHONE_AND_TERMS`，请本人在**本工具刚打开的窗口**操作：原神为 `act.mihoyo.com/ys/event/calculator/` 的养成计算器，绝区零为 `www.miyoushe.com/zzz/` 的米游社。本人输入手机号、阅读并勾选协议、点击一次“获取验证码”并输入短信验证码。默认程序不读取输入值或自动发送；其他 Chrome/内置浏览器窗口中的登录不会认证本工具的独立会话。

可选 `--login-method phone --phone-stdin --allow-sms`：显式授权一次代码发码，需新登录时从终端隐藏输入手机号；非交互调用用一行 stdin，并同时按既有要求提供 `--non-interactive --allow-login`。号码格式为国服 11 位手机号，只留本次内存、不写配置/日志或命令行；输入失败不回显号码。程序在唯一可见的官方 SDK 手机号框填写一次，显示 `PHONE_ENTERED`；未获代勾授权时显示 `WAITING_FOR_TERMS`，已获授权并完成勾选时显示 `TERMS_ACCEPTED`。本人阅读《米哈游用户协议》《米哈游隐私政策》，包括页面提示的未注册手机号验证通过自动注册；未单独授权时本人勾选，已有明确授权时可用 `--allow-terms` 代勾已识别的协议。未知协议/额外权限或控件歧义转 `PHONE_AUTOMATION_MANUAL`，不猜坐标。

本人勾选或按单独授权代勾后，程序至多点击一次“获取验证码”，显示 `SMS_SUBMITTING`，此时**尚未证明请求提交或短信发送**。发送控件不确定显示 `SMS_ACTION_UNCERTAIN`，不重试；结束前仍未观察到 POST 会显示 `SMS_REQUEST_NOT_OBSERVED`。同一命令跨游戏及逐身份阶段复用授权；一次自动发码额度用完后不会因重登自动恢复：交互终端只询问是否额外允许一次填号及发码，明确允许后继续原登录窗口；拒绝则取消。非交互模式报 SMS_AUTHORIZATION_REQUIRED 后停止，须先补充本人授权。仍有效的登录方式及协议授权不重复询问。图片 CAPTCHA 由本人完成；辅助流程的本次短信验证码在 `OTP_INPUT_REQUIRED` 后通过隐藏输入临时交给程序代填，不提前收集或复用；需要重新运行的超时/取消不自动延续旧发码操作。

只有观察到实际官方 POST 才输出 `SMS_REQUESTING`；回执须为 HTTP 200、整数 retcode=0、布尔 `data.sent_new` 和非负整数 `data.countdown`。`SMS_ACCEPTED` 表示官方接受新发码，不证明手机送达；`SMS_COOLDOWN` 表示已有验证码/倒计时，未新发短信。点击、倒计时或单独 retcode=0 都不能证明发码。网络失败、畸形回执或请求后 30 秒仍无回执报 `SMS_RECEIPT_UNKNOWN`，读取单次回执最多 5 秒；不重试。官方拒绝或同一命令又观察到发码请求报 `SMS_REQUEST_REJECTED`，风险/限流保留原错误码。手动 CAPTCHA 期间不推进身份探测，清除后再检查已观察的回执。回执未知时停止本次流程并做既有关闭/清理，不以再次点击试探；固定状态不能替代手机送达确认。已观察发码请求时，即使同轮精确 Binding 已通过，也须在请求后 30 秒和剩余登录期限内完成回执判定；重复、未知、拒绝及风控/限流不会被身份通过掩盖，不取消未决回执后报登录成功。

图片 CAPTCHA 显示 `WAITING_FOR_MANUAL_CAPTCHA`，请本人在本工具打开的对应官方浏览器窗口点击图片或操作滑块：原神为 `act.mihoyo.com/ys/event/calculator/`，绝区零为 `www.miyoushe.com/zzz/`。星铁暂时无法获取，请不要使用其保留的登录/采集接口。AI 不自动选择、不解图、不绕过验证。程序只读取可见验证面板或图片/滑块操作提示，不读取图片、答案或输入值；等待期间保留同一登录页面、Context 和 Profile，暂停自动 Binding 探测，不自动发码或重复发送短信。本人操作官方页面仍可继续。

面板消失显示一次 `CAPTCHA_CLEARED`，只恢复原有等待/核验流程；本人继续完成发码、输入验证码及确认，精确 Binding 通过才算登录成功。面板取消/消失、短信倒计时均不能证明验证通过或短信实际送达，程序不自动重发。持续显示期间提示不随轮询重复，重新出现则提示新一轮等待；操控短暂超时在原阶段期限内恢复。图片验证等待计入本人登录总预算 `--login-timeout`，到期报 `LOGIN_TIMEOUT`，持续操控故障或窗口关闭报 `LOGIN_CONTROL_UNAVAILABLE`；Ctrl-C 取消并做既有清理，Profile 不删除。未知验证 UI 可能无法被识别；官方 Binding 风控/限流、未知协议和额外权限仍按原规则停止，不能为通过验收扩大请求白名单。

## 会话中断与恢复

共享认证保存在 `<认证根>/account-session-v1/`，Profile 为其 `browser-profile/`，同一命令跨游戏、角色共享。取消、超时或关闭失败后保留该范围，下次普通命令重新通过官方服务核验；不把文件存在当作登录有效。浏览器仍在运行或存在 Singleton 标记时，先检查确切的本工具浏览器，不删锁和运行标记来强行启动。

旧的按身份 Profile、onboarding 目录和旧结果保持原地；程序不读取、搬移、合并或删除。`--resume-discovery` 不再支持，返回 `DISCOVERY_RESUME_UNSUPPORTED`。新账号范围通过正常登录建立，不延续旧的短信操作授权。

官方 Binding 无受支持角色时显示 `NO_ROLES` 与 `NOT_COLLECTED`，继续其他选中游戏。全部无角色时返回非成功状态，不宣称完成采集。多个绑定角色自动逐一处理，保存范围仍按游戏和 UID 隔离。
## 会话检查与登录授权工作流

直接运行 `hoyo-roster`。工具先启动专用浏览器、检查官方会话和 Binding；有效时直接采集。会话缺失时，保持同一进程和窗口，依次询问：

1. 允许二维码登录、手机号登录，或取消。交互选择手机号包含临时隐藏输入、填写号码和获取一次验证码的允许。
2. 是否允许代勾已识别的《米哈游用户协议》《米哈游隐私政策》（默认否；未注册手机号验证通过将自动注册）。说明中的官方协议链接供本人先阅读。

收到答案后立即执行所选流程。允许代勾时，根据官方 SDK 中两份已授权协议的名称及官方链接勾选一次，不逐字比对整段文案，支持自定义复选框的可见控件；拒绝时等待本人手动勾选。二维码扫码确认和图片验证仍由本人在同一窗口完成；辅助手机号流程确认新发码后询问验证码，再由程序在同一窗口代填并提交一次。登录后自动发现 UID，再核验身份、双次采集、验证、保存并关闭浏览器。

非交互调用需先取得上述允许，再传入 `--non-interactive --login-method qr|phone --allow-login`。手机号自动填写/一次发码另加 `--phone-stdin --allow-sms`，号码仅从 stdin 临时读取；**单独获准代勾协议才加 `--allow-terms`**。授权不涵盖未知协议或额外权限；页面变化或操作不确定时交还本人，不重复勾选/发码。

## 辅助短信验证码输入

交互选择辅助手机号，或明确允许后使用 `--phone-stdin --allow-sms`，会在本次 `SMS_ACCEPTED` 后核验官方 SDK 中唯一的短信登录表单、已确认协议、6 位验证码框和登录按钮，再输出 `OTP_INPUT_REQUIRED`。通过 Codex 使用时，助手应立即询问本次收到的验证码，把回答仅交给当前 CLI 的隐藏 stdin；普通终端直接隐藏输入。不要把验证码放进命令行、脚本、日志或截图。聊天中提供的验证码仍属于聊天内容，工具不额外持久化它。

验证码临时代填一次后显示 `OTP_FILLED`，下一轮重新核对图片 CAPTCHA、回执、协议及控件，再点击一次“登录”，显示 `LOGIN_SUBMITTING`。两者都不代表认证成功，仍须精确 Binding、完整采集验证和实际关闭。未知结构、撤销协议、填写/提交不确定显示 `OTP_AUTOMATION_MANUAL`，交还同一窗口且不重试；非 6 位 ASCII 数字或输入不可用报 `OTP_INPUT_INVALID`，不回显。等待输入不阻塞 CAPTCHA、风控/限流、身份探测和总登录超时；取消/关闭清理隐藏读入并恢复终端回显。

仅本次确认新发码的辅助流程可自动索取验证码；`SMS_COOLDOWN`、未知/拒绝回执、旧会话和已用发码授权不触发索取，不自动补发或使用历史验证码。图片 CAPTCHA 仍由本人操作。

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
| `USER_WORKSPACE_BUSY` | 等同账号会话或同游戏结果任务退出，不删锁文件来绕过 |
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

浏览器关闭后执行 `--operation logout`：删除当前账号范围的共享 Profile，使所有游戏后续会话检查失效；角色数据和请求节流保留。旧按身份 Profile 不受影响。发现 Singleton 标识时停止，要求先检查是否仍有浏览器进程。退出登录不会退出其他应用的米游社会话。

卸载代码：`python -m pip uninstall hoyo-roster`；这不删除任何个人结果或 Playwright 浏览器。需要彻底清理时，关闭所有本工具进程，按 privacy 文档核对范围后自行删除选定身份范围；保留数据或先做私有备份。不要把整个共享父目录作为清理目标。

可见登录与 `--headless` 会话复用使用同一个已选定的完整浏览器 channel，避免两个浏览器混用同一 Profile。历史 Mac Chromium 合成交接证据见 [Profile 验证记录](browser-profile-validation.md)，系统浏览器选择验证见 [浏览器选择验证](browser-selection-validation.md)；它不延长官方 Cookie 期限，也不自动恢复已失效认证。

本轮登录流程的公开静态证据、合成回归及真实验收边界见 [登录流程验证记录](login-workflow-validation.md)。

## 固定版本验收预检

关闭工具后更新并重新安装选定代码。验收时用该环境的 Python 运行 `python tools/verify_installation.py`，不要设置 `PYTHONPATH=src` 来掩盖安装差异。只有 `source_worktree_clean` 和 `installation_matches_source` 都为 true，才将正式 CLI 结果归于所报 source_sha；版本号相同也可能安装了旧代码。该检查只读取源码/安装包及 Git 状态，不读取认证内容。

## 普通控件自动等待与协议识别

普通手机号、协议、验证码控件或发送/登录按钮短暂未挂载或不可用时，先显示 WAITING_FOR_LOGIN_CONTROLS，按固定 CONTROL_STAGE 和 CONTROL_REASON 说明阶段，最多等待 15 秒，并受原登录总预算限制。控件出现后自动继续，无需重复询问已有授权或让本人重新填号。未知结构持续不恢复才进入对应接管状态，实际填写或点击结果不确定仍不重试。

只要求相关可见表单内控件唯一，不因无关 SDK frame 阻断。协议识别两份已授权名称和官方链接，接受已/我已、空白、标点、二维码缺少自动注册说明以及 hideBack 参数不同或缺失。原生 checkbox 不可见时操作同一自定义 checkbox 的可见 SDK 控件，并核对勾选状态。新增协议/额外权限、非官方目标、真正有歧义的控件、风控和限流仍停止；官方 Binding 身份核验不变。

短信服务拒绝报 `SMS_REQUEST_REJECTED`；观察到重复短信请求报 `SMS_DUPLICATE_REQUEST`。两者均终止当前认证尝试，不自动重发；错误不包含响应体或输入值。
