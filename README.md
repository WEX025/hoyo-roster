# hoyo-roster

hoyo-roster 将你本人国服原神或绝区零的受支持账号状态保存为本机私有 JSON 文件。四个业务模块为登录、原神、崩铁、绝区零，各游戏负责自己的采集与保存。

**崩坏：星穹铁道暂时无法获取，请不要使用。原神目前不含圣遗物和完整属性面板。**

## 登录与采集工作流

默认原神→绝区零；各游戏先检查同一 AI 客户端浏览器中的登录态，缺失或失效时才调用登录模块。

客户端宿主可显式设置 `parallelGames: true`，在同一任务中交叠两游戏页面采集；每游戏内部仍串行，共享会话锁和两秒来源间隔。默认串行用于兼容和对照，页面并发不代表其他客户端或 API 路径已经验收。详见[客户端桥接](docs/client-bridge.md)。

登录使用当前 AI 客户端的内置浏览器，与操作系统无关；Codex 使用 Codex 内置浏览器。AI 打开官方登录页，提示你自行登录，并主动询问是否完成。你确认后登录模块结束，将同一会话交给当前游戏采集模块。手机号、验证码、扫码、协议和图片验证都由你在页面操作。后续游戏复用同一会话。

游戏身份核验与数据是否可读由游戏模块负责。登录模块结束不代表采集完成；第二个游戏发现会话失效时，再调用同一个登录模块即可。详见[四模块工作流](docs/workflow.md)。

## 当前实现状态

**默认通过内置浏览器的官方页面采集并保存 `partial` 页面记录；不要求 CDP。页面记录的字段范围与完整 API 快照不同。**

[local-roster skill](.agents/skills/local-roster/SKILL.md) 通过包内 client_runtime.mjs 的 createRosterHost(browser) 装配游戏读源实现、客户端宿主和 Python 私有管道；Python 不自行启动浏览器。普通终端运行在线命令会报 CLIENT_BROWSER_REQUIRED。安装包不会自动获得客户端浏览器能力。

页面路径读取官方角色面板身份、已拥有名单及对应当前详情。原神保存页面同步后的等级、三项技能和当前武器；绝区零保存等级及明确标为当前的音擎、技能、驱动盘区块，排除未拥有角色和推荐内容。两轮一致且身份稳定后原子保存；结果明确为 `OFFICIAL_PAGE / PARTIAL`，不生成未经证明的官方数字 ID，不覆盖 API 完整快照。[页面字段和保存格式](docs/page-observations.md)是此路径的范围说明。

原有 API 读源器仍保留历史请求/响应监听、签名与绝区零游戏服务初始化；仅在客户端提供相应能力时运行，完整覆盖要求保持不变。游戏分别检查自身来源，缺少 CDP 不再阻断可用的页面路径。低层宿主未装配适配器报 SOURCE_ADAPTER_MISSING。详见[客户端桥接](docs/client-bridge.md)和[验证状态](docs/release-validation.md)。

Windows 与 macOS 的当前版本验收状态统一见[当前平台状态](docs/release-validation.md#当前平台状态)，历史通过记录按源码版本保存。其他客户端、Linux 客户端浏览器、真实客户端重启保持和完整 API 采集尚未验收。版本 0.1.0rc1 仍为发布候选。

## API 快照字段范围

| 游戏 | 可保存的信息 | 能力边界 |
|---|---|---|
| 原神 | 来源支持的等级、突破、命座、好感、技能和当前武器等字段 | **不含圣遗物和完整属性面板**。`partial` 表示字段覆盖有限；角色名单仍须通过闭合核验，不能按残缺名单保存 |
| 绝区零 | 代理人等级、晋阶、影画、技能、音擎、驱动盘、面板和邦布 | 只有名单、养成名单、逐角色详情、批量详情和邦布的覆盖条件全部满足，才输出 `complete`；不表示账号的所有信息 |
| 崩坏：星穹铁道 | 当前不可用 | **暂时无法获取，请不要使用**。保留的离线格式和合成测试不代表采集能力 |

此表描述原有 API 格式；默认页面采集的范围见[页面记录](docs/page-observations.md)，其状态始终为 `partial`。

## 安装与离线维护

需要固定版本、前台分段续跑、逐步计时和串并行复盘时，使用[持久化前台工作流](docs/timed-collection.md)的统一入口，自动完成采集、收尾、验证及报告；支持已提交游戏检查点恢复，入口随 wheel 分发。

Python 包要求 Python 3.12。安装后可对明确选择的已有结果执行离线检查和恢复；登录无需预先提供 UID。

    python3.12 -m venv .venv
    source .venv/bin/activate
    python -m pip install .
    hoyo-roster --game genshin --uid YOUR_UID --operation inspect

Windows 可使用 py -3.12 -m venv .venv 与虚拟环境中的 Scripts/python.exe，无需激活脚本或修改系统执行策略。本机路径、结果检查和恢复见[本机使用](docs/local-use.md)。安装 Python 包不会给它建立客户端内置浏览器连接。

## 输出与隐私

每次同步先核验所选游戏、UID 和区服，再采集两次；业务结果和来源覆盖一致、数据通过验证后才保存。不同游戏的字段范围见上方支持矩阵；只保存当前已支持的装备字段，不保存库存、货币、资源或推荐配装。

从本项目源码运行或安装，结果默认集中在仓库 `data/`，按游戏和 UID 派生哈希隔离。页面结果在 `page-observations-v1` 的私有 `snapshot.json`；API 结果仍在 `game-results-v3`，两者互不覆盖。程序核验源码仓库，不选择 cwd 中的任意仓库；独立 wheel 无可核验源码时回退到系统用户数据目录 `hoyo-roster/results`。不会写入 `site-packages`。`--storage-root /absolute/private/results` 只覆盖结果位置。

系统用户数据根由 platformdirs 统一选择：macOS 为 `~/Library/Application Support/hoyo-roster`，Windows 为 `%LOCALAPPDATA%\hoyo-roster`，Linux 为 `$XDG_DATA_HOME/hoyo-roster`（未设置时 `~/.local/share/hoyo-roster`）。独立 wheel 的结果放在该根的 `results/` 下。

页面结果报告 `RESULT_SCOPE` 和指向 `snapshot.json` 的 `CURRENT_RESULT`。API 路径另报告 `CURRENT_POINTER`，其结果目录包含 `profile.json`、`roster.json`、`builds/` 和绝区零的 `bangboos.json`。哈希不是加密，完整本机路径也不要贴到公开反馈。

仓库只跟踪 `data/.gitkeep`，个人结果由 `.gitignore` 忽略。普通 `git add .` 不带出这些结果，但忽略规则不能保护已经跟踪或强制添加的文件，也不保护整个目录的打包分享。提交前运行 `python tools/check_private_data.py`，检查只读取 Git 路径列表，不读取私人内容；禁止提交个人结果。

正常同步只保存本机数据，不修改源码，不提交或上传个人结果；浏览器仍会与官方页面及其引用的服务通信。客户端浏览器自行管理敏感登录状态；本工具的 runtime 目录只存锁和节流元数据。浏览器登录材料不属于角色结果，不应收集、输出或上传。程序输出不打印 UID 或凭据，但你输入的 UID 可能留在命令行历史和进程参数中。

反馈问题时只提供版本、平台、固定错误代码和脱敏步骤，不附真实 UID、手机号、验证码、登录材料、截图、原始响应或个人 JSON。清理和卸载前先核对确切身份范围，保留有效数据或做好私下备份；详见[隐私、存储和清理](docs/privacy.md)。



--runtime-root 仅控制仓库外私有锁与节流元数据，不保存登录态。登录态由客户端浏览器管理，交接时保留同一会话；不导出登录材料，也不承诺退出客户端后仍可复用。旧账号认证和结果目录保留原地，不搜索、不迁移、不清理。

## 许可证与开发说明

本项目代码和项目文档采用 [MIT 许可证](LICENSE)，版权声明为 © 2026 WEX025。依赖的许可证和保留通知见[第三方依赖声明](THIRD_PARTY_NOTICES.md)。本项目不是米哈游官方产品，不包含游戏素材；MIT 许可证不授予官方素材、商标或接口的使用权，官方服务及页面行为仍以其自身条款和提示为准。

参与开发前请阅读[开发指南](CONTRIBUTING.md)。项目代理规则以 [AGENTS.md](AGENTS.md) 为权威，[CLAUDE.md](CLAUDE.md) 只指向该文件。当前验证范围及尚未完成的真实采集和平台验收见[发布与验证记录](docs/release-validation.md)。
