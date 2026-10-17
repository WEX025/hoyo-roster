# 持久化的前台采集工作流

`src/hoyo_roster/client_workflow.mjs` 的 `openCollectionWorkflow` 是原神和绝区零页面同步的统一入口，随 wheel 和源码一起分发。顶层控制器串起环境准备、来源版本固定、采集、收尾、快照验证和报告；[游戏规则](../AGENTS.md)继续由原有核心执行。`tools/timed_collection.mjs`、`tools/analyze_collection.py` 保留兼容入口，不作为新的执行流程。

## 配置和调用

运行 `hoyo-roster-workflow --entrypoint` 可获得实际包入口 file URL 和解释器；`hoyo-roster-workflow --status ABSOLUTE_RUN_DIRECTORY` 只读固定状态。在当前客户端的主动浏览器工具调用中导入该包路径。客户端工具负责取得真实内置浏览器对象；不由本项目启动另一个浏览器。

```js
var { openCollectionWorkflow } = await import(PACKAGE_FILE_URL + "/client_workflow.mjs");
var workflow = await openCollectionWorkflow({
  directory: NEW_PRIVATE_RUN_DIRECTORY,
  parallelGames: true,
});
console.log(await workflow.advance(browser));
```

`PACKAGE_FILE_URL` 指向源码 `src/hoyo_roster` 或实际安装包目录，目录参数为私有 run 目录。必填配置只有 directory；不从 cwd 猜仓库、不扫描用户目录。默认串行，示例显式启用跨游戏并发。Python 从安装环境、源码 .venv、已知 Python 命令中检测 3.12；歧义时显式传 python。缺少基础依赖时尝试安装选中的公开包；无法建立安全控制环境时返回固定错误，不假装已经创建可恢复任务。

最小依赖引导与正式 PREPARING 共用 installation 的单次 Git 信任、stdin 隔离与私有临时目录策略。引导临时目录是所选 run 的新建私有同级目录，不提前创建 workflow.json 或复用旧 runtime；安装后核验依赖并只清理可证明归属的临时范围。Windows 必须先具备 `pywin32==311` 原生权限支持；缺失时明确返回 `WORKFLOW_PLATFORM_DEPENDENCIES_REQUIRED`，由环境安装步骤补齐依赖，不用 chmod 或继承 TEMP 降低权限要求。`repairInstallation:false` 仍禁止自动安装。正式阶段继续固定源码、逐文件核对并修复安装，不复制第二套业务编排。

其他可选参数：

| 参数 | 默认与含义 |
| --- | --- |
| python | 已知位置自动发现；显式值选择实际 3.12 解释器 |
| runtimeRoot | 平台用户私有根；覆盖时跨 run 复用同一路径，不能每游戏各建一套锁 |
| update | false；仅用户要求更新时传 true，检查干净工作区后 fetch 并只允许快进当前 upstream；不切分支、不 reset |
| repairInstallation | true；已匹配就跳过；不匹配时重装并逐文件复核 |
| installationArtifact | 可选的本地 wheel，供离线修复安装；安装后仍须与目标源码逐字节匹配 |
| baseline | 明确选择的旧 run 目录；自动核对数据并比较采集耗时 |

复用已有 directory 时只传 directory 和必要的 python；其他配置不可悄悄更改。预检失败是显式失败状态，不会带着旧包继续。更新仅发生在第一次准备阶段；恢复禁止切换采集版本。公开 JS 被复制到该 run 内按内容哈希隔离的代码目录，避免长驻 ESM 缓存执行旧代码；不复制浏览器状态。

## 固定状态机

| 状态 | nextAction | 行为 |
| --- | --- | --- |
| PREPARING | CONTINUE | 源码状态、授权更新、安装检查/修复、固定 runtime |
| WAITING_BROWSER | ATTACH_BROWSER | 等待实际浏览器；已附加时返回 CONTINUE |
| COLLECTING | CONTINUE | 同一 worker 执行各游戏身份、双轮、校验、原子保存 |
| WAITING_LOGIN | CONFIRM_LOGIN | 挂起在人工登录；使用当前 opaque attempt 回复 |
| VERIFYING | CONTINUE | 验证快照哈希、字段、身份和当前尝试的复核记录 |
| REPORTING | CONTINUE | 自动生成本机结构化及 Markdown 报告 |
| SUCCEEDED | DONE | worker 已关闭、设置恢复、验证与报告均完成 |
| FAILED / CANCELLED | STOP | 固定错误或明确取消；不能报告 PASS |

每次只调用 `await workflow.advance(browser)`，控制器选择阶段；不再另外启动分析命令。准备阶段单独返回；采集阶段在一个主动调用里连续推进，最多 180 秒启动预算，内部每窗十秒并收拢最长45秒来源，完成后自动验证和报告。测试桌面采用 270 秒工具超时。主动调用返回后不继续浏览器工作。不得仅后台等待 worker completion；不得用两个任务绕过浏览器占用、共享锁或两秒节流。

登录立即返回 question，用户在官方页面处理全部凭据和验证。只有本人针对当前 `question.attempt` 明确确认后才能调用 `workflow.answerLogin("completed", question.attempt)`。waiting 保留相同问题并等待，不重复询问；cancel 调用 `await workflow.cancel()`。详见 [Skill](../.agents/skills/local-roster/SKILL.md)。控制器拒绝错尝试、换浏览器及并行 advance。收尾保留共享浏览器，不自动关闭页面。

## 持久化与恢复

`workflow.json` 使用已有私有权限、排他锁及原子元数据写入，保存不可变配置、源码/包摘要、阶段、尝试、身份指纹、已提交快照路径与哈希。不保存真实浏览器引用、认证材料或登录确认。MD 不是恢复状态来源。

游戏首次读官方身份时将身份指纹固定；双轮一致、校验通过且 store.save 成功后才写该游戏检查点。源数据、身份或持久化失败不成为已完成记录。恢复先重新建立客户端宿主并读取官方身份；已提交游戏还须验证原快照哈希和格式，只复核身份、不重复收集；未提交游戏重新执行两轮，拒绝混合未完成轮次。

新进程恢复调用相同 `openCollectionWorkflow({directory, python?})`，随后继续 advance。旧控制器、准备进程或 worker 仍活着会拒绝接管，不盲杀 PID。旧 worker 随私有管道断开退出、锁释放后才能继续。若旧客户端进程仍活着但对象绑定丢失，返回 BUSY 是安全限制，需要恢复原对象或由本人处理原进程；不能虚构跨客户端重启保持。

验证/报告阶段中断可离线重做，报告写入后才进入 SUCCEEDED。明确取消保持 CANCELLED；只有用户恢复请求才使用 retry。retry 仅允许列明的临时失败，身份变化、风控、限流及检查点损坏不允许盲重试。版本变化需要新的任务，不能在旧记录上混跑新代码。

## 官网导航的有限恢复

公共页面层的 navigate 在自己执行的官方 HTTPS 导航/刷新中，只对明确的 DNS/连接重置/连接关闭/超时/网络变化错误补试一次，目标仍为注册的同一官方 URL。也识别这次导航落入的错误页 URL 元数据中的固定网络码，不读取被客户端禁止的错误页 DOM。补试共用当前来源的45秒截止和取消信号；取消、认证/身份/风控/数据错误、证书/SSL错误及未知跨域跳转不进入恢复。page.navigation-retry 单独计时，原失败仍记为失败跨度。

补试在同一个 browser 中新建一个该原址标签，并保留原标签，因为部分客户端连在 data: 错误标签上执行正常导航都会拒绝。navigate 返回实际就绪的标签，游戏更新本轮标签引用并重新读取官方身份；公共页面层保留新引用供后续来源使用。不复制认证材料、不换浏览器、不改网络/安全设置、不反复开页。新标签仍失败则正常停止。

reload 返回成功不证明页面已经可读：会继续通过只读 DOM scope 核对当前文档 URL。若 scope 建立失败，再从同一浏览器的实时标签列表确认这次导航对应的错误页固定网络码；没有该证据就原样失败，不从任意读取失败猜测网络原因。实际可见文档已跳到未知来源时直接停止，不用迟到的错误元数据覆盖它。

仍失败时正常 STOP，保留 worker 收尾及检查点。先确认原官方页面能通过有限刷新恢复；用户明确要求继续后，对允许重试的临时失败调用同一 workflow.retry()，未提交游戏重做双轮，已提交游戏只重新核验身份。不要连续新建标签或反复重启任务。无法恢复再请求用户检查网络；未知原因不伪装成需要重新登录。

不凭猜测替换路径、域名或地区站点。新地址须来自官方可验证入口，并单独验证来源 URL、同一游戏/区服身份、页面字段和双轮结果后，才能修改游戏的注册来源；同一主机换路径不构成 DNS/协议故障的可靠修复。ERR_DNS_NO_MATCHING_SUPPORTED_ALPN 的协议选择背景可见 [Chromium 实现](https://chromium.googlesource.com/chromium/src/+/HEAD/net/http/http_stream_pool_attempt_manager.cc)，不能仅据错误码认定是用户配置错误。

## 产物和计时

| 文件 | 用途 |
| --- | --- |
| workflow.json | 权威持久化状态及每游戏检查点 |
| workflow-steps.md | 预检、采集、人工等待、验证、报告的逐阶段墙钟 |
| attempts/NNNN/version.json | 该尝试固定版本 |
| attempts/NNNN/events.jsonl、steps.md、result.json | 每窗口/pump/来源指标和真实 worker 关闭结果 |
| results/ | 经过验证、原子保存的个人页面快照 |
| report/summary.json、summary.md | 自动生成的最终报告、计时与可选基线对比 |

全流程墙钟与采集尝试墙钟分别报告。人工等待、中断和交接空档计入所属阶段；中断前尚未落盘的浏览器跨度标为不完整，不补造数据。不同进程 performance.timeOrigin 不同，只在各次尝试内部计算跨度并集与交叠，不能跨进程直接合并。嵌套跨度不相加冒充端到端。

公共页面层为 DOM evaluate 使用客户端支持的五秒 scope 建立/执行超时，并分别记录页面枚举、绑定、视口读取与 URL 读取的固定指标。来源仍受统一45秒截止约束；客户端传输若未及时返回，控制器必须等待在途调用收拢，不用新的浏览器动作绕过取消，也不把未收拢的截止时间称为硬性墙钟上限。

旧实验记录仍可用 `hoyo-roster-analyze --run RUN_DIRECTORY --compare BASELINE_DIRECTORY --output REPORT_DIRECTORY` 离线分析；标注 replay，不当作新增真实采集。正常工作流不需要这一步。更换源码、数据范围或恢复任务后的性能差值不当作同条件提速。

## 验证边界

### Windows

Windows 使用同一入口和 `parallelGames:true`，Python 由所选虚拟环境的 `Scripts/python.exe` 提供。`platform_io.launch_parent_pid()` 在 Windows 只跳过与 `sys.executable` 精确匹配的一个 venv redirector，使用进程句柄核对存活、映像和创建时间；控制器与 worker 都走此函数，仍拒绝不同控制器。无法核验时停止，不枚举任意祖先进程来放行。Node 启动辅助进程使用 `windowsHide:true`，不弹出额外终端。

客户端沙箱 SID 可能与显式选择的源码仓库所有者不同。Git 命令只在本次调用信任模块所锚定的确切 checkout，不写全局 `safe.directory`；来源无法核验或要求更新却没有源码时明确失败。普通安装通过 PEP 610 核验本地源码后同样记录 SHA，并逐文件比较安装内容。

工作流安装与修复不复用 pip 缓存，避免前一次由另一 Windows 身份生成的缓存阻断重装。快照检查点与最终验证在平台层统一内部路径前缀，支持普通入口路径与 NTFS 长路径表示之间的比较，不改变相对检查点格式或放宽目录边界。

安装子进程的 TEMP/TMP/TMPDIR 指向本次 run 的私有 `preparation-temp`，不依赖客户端传入的跨身份临时目录。Windows 的 CLI/worker 通过 `platform_io.run_async` 使用 Python 3.12 IOCP 和本地命名管道唤醒，避免标准 socketpair 的回环 TCP 初始化阻塞。此适配集中在 `windows_io`，保留原有异步任务、私有桥接、取消和并发限制；不更改全局事件循环策略或网络权限。由于 Windows 信号唤醒 fd 仅支持 socket，IOCP 空闲等待上限为 100ms，由 Runner 正常处理 SIGINT；回归覆盖跨线程唤醒、满缓冲、取消和句柄关闭。

Windows 回归须包含真实 venv 的 Node→控制进程→worker→双轮→保存→验证→报告，测试不同游戏实际交叠、同游戏串行和共享两秒节流；恢复测试另验证已提交快照不重抓。合成来源通过后，再通过当前客户端内置浏览器进行本人官网采集，并在验收记录中分别标注。其他 OS 的实机验证不能由 Windows 结果替代。

`tests/test_collection_workflow.py` 运行实际身份、双轮、规范化与保存，覆盖恢复、账号/快照变化、状态机和收尾门槛。`tests/test_client_workflow_driver.mjs` 检查单入口、人工等待、取消及离线报告衔接。`tests/test_client_workflow_integration.mjs` 使用真实控制进程、worker、管道和保存，在第一个游戏提交后退出控制进程，再由另一个进程恢复；只替换官方来源为合成数据。

真实官网验收应单独记录源码版本、平台、阶段计时、角色数量和结果范围。合成进程恢复测试不等于真实桌面客户端重启验收。所有个人产物仅存本机；不因工作流完成而自动创建 PR、推送、发布或合并。
