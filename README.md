# hoyo-roster

hoyo-roster 将你本人国服原神或绝区零的受支持账号状态保存为本机私有 JSON 文件。

**崩坏：星穹铁道暂时无法获取，请不要使用。原神目前不含圣遗物和完整属性面板。** 星铁数据格式和离线接口保留；`hsr` 登录/同步入口报 `GAME_PAUSED`，不代表当前可用；本说明不推断上游原因。

当前版本 **0.1.0rc1 是发布候选**。指定版本的真实验收、合成回归及未验收范围见[发布与验证记录](docs/release-validation.md)；合成通过不代表最新真实界面已验收。

## 当前支持范围

| 游戏 | 可保存的信息 | 能力边界 |
|---|---|---|
| 原神 | 来源支持的等级、突破、命座、好感、技能和当前武器等字段 | **不含圣遗物和完整属性面板**。`partial` 表示字段覆盖有限；角色名单仍须通过闭合核验，不能按残缺名单保存 |
| 绝区零 | 代理人等级、晋阶、影画、技能、音擎、驱动盘、面板和邦布 | 只有名单、养成名单、逐角色详情、批量详情和邦布的覆盖条件全部满足，才输出 `complete`；不表示账号的所有信息 |
| 崩坏：星穹铁道 | 当前不可用 | **暂时无法获取，请不要使用**。保留的接口和历史测试不是使用建议 |

开发与安装的历史验收平台为 macOS arm64、Python 3.12。本分支已加入原生 Windows 适配；Windows 11 x64 上 Chromium 的本人真实流程曾通过，系统 Edge 的原生离线复用通过，**当前默认 Edge 的真实端到端、macOS 系统 Chrome 及 Linux/WSL 实机验收仍未完成，不宣称跨平台正式可用**。Windows 当前目标为 Python 3.12、已安装的 Edge（Chromium 备用）、本地固定 NTFS 盘，不支持网络共享、FAT/exFAT、reparse/junction 或云同步目录。详见[平台边界与验收](docs/platform-support.md)及[系统浏览器验证](docs/browser-selection-validation.md)。当前可用游戏仅支持明确允许的中国大陆区服：原神 `cn_gf01` / `cn_qd01`、绝区零 `prod_gf_cn`。国际服、其他区服和其他登录提供方不在当前支持范围内。

更详细的字段、数据格式和完整度说明见[数据格式和完整度](docs/data-format.md)。

## 安装与快速开始

```bash
git clone --branch main https://github.com/WEX025/hoyo-roster.git
cd hoyo-roster
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .
# macOS 未安装 Chrome 时需要；Linux 必须安装；也可作为备用：
python -m playwright install chromium
hoyo-roster --game all
```

Windows PowerShell（验收 PR 时先切换到该 PR 分支；无需激活脚本或修改系统执行策略）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe tools\platform_smoke.py
.\.venv\Scripts\python.exe -m hoyo_roster --game all
```

离线 smoke 仅用新临时目录检查权限、锁、保存和恢复，不登录、不发短信，不访问已有结果或认证目录。Windows 安装失败或 smoke 失败时先停止，报告固定错误代码与平台；不要用管理员运行或放宽 ACL 绕过。真实验收顺序见[平台验收](docs/platform-support.md#本人-windows-验收)。

首次使用直接运行 `hoyo-roster`：先在官方页面登录，再自动发现本人原神和绝区零的 UID，并逐身份核验、同步；**无需提前查找、填写或提供 UID，Windows、macOS、Linux 使用同一流程。** `--game all` 与默认相同，`--game genshin` / `--game zzz` 只发现对应游戏。`--operation discover` 只发现并保存本机角色选择，不采集角色数据。再次运行读取私有选择文件，但仍重新验证官方身份，选择文件不能证明会话有效。多个角色逐一处理，不默认挑选其中一个。星铁仍暂停。

`--uid` 仅是已有明确身份时的可选精确选择，也用于指定身份的离线检查/恢复，不是首次使用前置条件。即使显式提供 UID，是否属于当前登录账号仍须由官方账号绑定信息核验。

`hoyo-roster` 和 `python -m hoyo_roster` 使用同一个入口，无需配置 `users.toml`、私有数据仓库、后台服务或 GitHub 登录。新会话在 Windows 优先使用已安装的 Edge，在 macOS 优先使用已安装的 Chrome，Linux 使用 Playwright Chromium。首选浏览器缺失或首次启动失败时尝试 Chromium；Chromium 未安装则明确停止，请在同一虚拟环境运行 `python -m playwright install chromium`（Windows 用 `.\.venv\Scripts\python.exe -m playwright install chromium`）。程序不自动安装或覆盖系统浏览器，发行包也不包含 Chromium。

每个身份使用工具专用的私有会话，不读取日常浏览器登录态。首次选定浏览器后保存选择，可见登录和无界面采集复用同一浏览器；已有会话启动失败、登录失效、验证码、身份冲突和限流都不自动换浏览器。旧的无选择标记 Profile 保留并停止，不自动迁移；新会话需显式使用新的 `--auth-root`，首次由本人登录。详见[本机使用指南](docs/local-use.md#浏览器选择和会话复用)。

```bash
hoyo-roster
hoyo-roster --game genshin --operation login --login-method qr
hoyo-roster --game zzz --operation discover
```

默认操作为同步。`--operation login` 只检查登录和精确身份，不采集角色；`--operation inspect` 检查本机数据状态。完整的首次使用、升级与恢复步骤见[本机使用指南](docs/local-use.md)。

## 二维码与短信登录

会话有效时，程序直接复用该游戏和 UID 的独立会话。需要重新登录时，终端先说明本人配合、官方通信、本机会话保存及协议边界，再询问 **二维码 / 手机号 / 取消**。

二维码需要本人扫码并确认；手机号方式需要本人在官方页面输入手机号、请求短信、输入验证码并确认。必要的协议和隐私条款也由本人阅读、勾选，程序不自动接受。出现未知新协议或额外权限时，应暂停并另行确认。实际登录入口以官方页面为准；没有观察到入口，不等于官方不支持该方式。

可以用 `--login-method qr` 或 `--login-method phone` 选择方式，用 `--login-method cancel` 取消新登录。仅选择方式不等于已经允许登录；交互终端仍会询问。非交互调用需要先取得本人允许，再显式提供 `--non-interactive`、`--allow-login` 和登录方式，例如：

```bash
hoyo-roster --game zzz --non-interactive --allow-login --login-method phone
```

`--allow-login` 只允许说明中的本机手动登录与会话复用，不替代本人确认官方协议，也不授予未知新权限。`--headless` 只适用于已登录且身份匹配的会话；需要新登录时会停止。

## 图片验证码由本人操作

遇到图片验证码（CAPTCHA）时，程序显示 `WAITING_FOR_MANUAL_CAPTCHA`，并说明对应的官方浏览器登录窗口：原神养成计算器或绝区零米游社。**请本人自己点击图片或操作滑块，AI 不自动选择、不解图、不绕过验证。**

保持该窗口和命令运行。等待期间，程序保留同一会话，暂停自动身份探测，不自动发码或重发短信。验证面板消失后显示 `CAPTCHA_CLEARED`，随后继续等待本人完成发码、登录和精确身份核验。**面板消失不代表验证或登录成功，页面倒计时也不代表短信实际送达。**

等待期间可按 Ctrl-C 取消。各登录等待阶段默认上限为 900 秒，可用 `--login-timeout` 设置，范围为大于 0 且不超过 3600 秒；图片验证等待计入所在阶段。超时或窗口关闭会停止本次操作，保留会话目录供重新运行时核验复用。其他风控和限流仍按原规则停止。

## 状态提示

程序输出固定状态标识，并附中文提示。下表列出主要阶段；更多错误代码见[本机使用指南](docs/local-use.md#常见停止和建议)。标识保留原样，方便反馈问题。

| 标识 | 含义或下一步 |
|---|---|
| `CHECKING_SESSION` / `INITIALIZING_SESSION` / `VERIFYING_IDENTITY` | 检查会话、初始化官方页面并核验游戏、UID 和区服 |
| `WAITING_FOR_ENTRY` | 等待所选官方登录入口加载，请在浏览器中继续操作 |
| `CONTROL_RETRY` | 浏览器操控暂时超时，在当前期限内等待恢复 |
| `WAITING_FOR_MANUAL_CAPTCHA` | 等待本人在提示指向的官方窗口完成图片验证 |
| `CAPTCHA_CLEARED` | 验证面板已消失，继续等待登录和身份核验；不代表短信已送达 |
| `COLLECTING_1` / `COLLECTING_2` / `VALIDATING` | 采集两次并验证结果及来源覆盖 |
| `SAVING` / `SAVED` | 正在保存 / 数据已保存 |
| `UNCHANGED` | 数据无变化，保留原业务时间戳 |
| `CLEANUP_PENDING` | 保存已成功，清理尚未完成；按指南检查并执行恢复 |
| `LOGIN_TIMEOUT` / `LOGIN_CONTROL_UNAVAILABLE` | 等待超时或浏览器操控不可用，检查官方窗口后重新运行 |
| `WRONG_ACCOUNT` / `RATE_LIMITED` / `RISK_CONTROL` | 身份不匹配、限流或风控，停止本次操作，按官方提示处理 |

## 输出与隐私

每次同步先核验所选游戏、UID 和区服，再采集两次；业务结果和来源覆盖一致、数据通过验证后才保存。不同游戏的字段范围见上方支持矩阵；只保存当前已支持的装备字段，不保存库存、货币、资源或推荐配装。

从本项目 Git 仓库源码运行，或按上方步骤克隆后执行 `pip install .` / `pip install -e .`，结果默认集中在该仓库的 `data/`，按游戏和 UID 派生哈希隔离。程序从源码位置或安装时记录的本地源码地址核验本项目仓库；从仓库子目录或其他当前目录运行仍使用同一结果根，不选择当前目录里的任意仓库。单独安装 wheel、源码仓库已删除或无法核验时，结果回退到 系统用户数据目录下的 `hoyo-roster/results`（具体位置见下文），不会写入 `site-packages`。`--storage-root /absolute/private/results` 只覆盖结果位置。

系统用户数据根由 platformdirs 统一选择：macOS 为 `~/Library/Application Support/hoyo-roster`，Windows 为 `%LOCALAPPDATA%\hoyo-roster`，Linux 为 `$XDG_DATA_HOME/hoyo-roster`（未设置时 `~/.local/share/hoyo-roster`）。独立 wheel 的结果放在该根的 `results/` 下。

浏览器 Profile、认证 key/指纹、认证锁和节流留在仓库外的上述系统用户数据根，按游戏/UID 隔离；可用 `--auth-root /absolute/private/auth` 明确覆盖认证根，必须在仓库外。本项目尚未发布，不提供历史逻辑兼容或数据迁移；既有目录保留原地，程序不搜索、迁移或清理它们。目录策略见[本机使用指南](docs/local-use.md)。

终端显示 `RESULT_SCOPE`（身份结果目录）、`CURRENT_POINTER`（当前指针）和 `CURRENT_RESULT`（当前 JSON 所在目录），据此打开 `profile.json`、`roster.json`、`builds/` 及绝区零的 `bangboos.json`。结果仍是当前快照，哈希不是加密；完整本机路径也不要贴到公开反馈。

仓库只跟踪 `data/.gitkeep`，个人结果由 `.gitignore` 忽略。普通 `git add .` 不带出这些结果，但忽略规则不能保护已经跟踪或强制添加的文件，也不保护整个目录的打包分享。提交前运行 `python tools/check_private_data.py`，检查只读取 Git 路径列表，不读取私人内容；禁止提交个人结果。

正常同步只保存本机数据，不修改源码，不提交或上传个人结果；浏览器仍会与官方页面及其引用的服务通信。会话目录包含敏感登录状态，不属于正式角色结果，不应收集、输出或上传。程序输出不打印 UID 或凭据，但你输入的 UID 可能留在命令行历史和进程参数中。

反馈问题时只提供版本、平台、固定错误代码和脱敏步骤，不附真实 UID、手机号、验证码、登录材料、截图、原始响应或个人 JSON。清理和卸载前先核对确切身份范围，保留有效数据或做好私下备份；详见[隐私、存储和清理](docs/privacy.md)。

## 常见问题

**为什么原神显示 `partial`？** 当前只覆盖已支持的有限字段，不含圣遗物和完整属性面板。这个状态不允许名单缺失；无法证明名单闭合或遇到未知多页数据时，程序会拒绝保存。

**可以尝试星铁的 `hsr` 接口吗？** 请不要使用。星铁暂时无法获取，接口、格式和历史测试只是暂时保留的技术内容，不代表可用，也不据此推断上游原因。

**页面开始倒计时，但没有收到短信怎么办？** 倒计时不能证明短信已送达。请本人查看官方页面提示；程序不会自动重发短信或代解验证码。

**浏览器打不开，或提示会话目录正在使用怎么办？** 检查本工具选定的 Edge/Chrome/Chromium 是否可用，若首次回退提示 Chromium 缺失，在同一虚拟环境安装它。检查本工具对应的浏览器是否仍在运行，不删除会话目录、锁或浏览器运行标记来绕过检查。错误代码与处理步骤见[本机使用指南](docs/local-use.md)。

**保存失败或有恢复待办怎么办？** 保留当前数据、恢复日志和相关代际，先检查状态，再按[恢复步骤](docs/local-use.md#检查与恢复)操作。不要按目录名或 UUID 盲删数据，也不要删除身份标记来绕过保护。

## 许可证与开发说明

本项目代码和项目文档采用 [MIT 许可证](LICENSE)，版权声明为 © 2026 WEX025。依赖的许可证和保留通知见[第三方依赖声明](THIRD_PARTY_NOTICES.md)。本项目不是米哈游官方产品，不包含游戏素材；MIT 许可证不授予官方素材、商标或接口的使用权，官方服务及页面行为仍以其自身条款和提示为准。

参与开发前请阅读[开发指南](CONTRIBUTING.md)。项目代理规则以 [AGENTS.md](AGENTS.md) 为权威，[CLAUDE.md](CLAUDE.md) 只指向该文件。历史验证结果及尚未完成的真实界面和平台验收见[发布与验证记录](docs/release-validation.md)。
