# 客户端浏览器桥接

## 正式入口

AI 在当前客户端的 JavaScript 运行时导入 src/hoyo_roster/client_runtime.mjs，调用 createRosterHost(browser)。browser 必须是已通过客户端文档选定的实际内置浏览器对象；不能传浏览器 ID，不能另开独立浏览器。这个装配入口直接连接两游戏的读源实现，不再要求执行 AI 临时编写 readSource。

默认优先页面 DOM 路径：page:identity、page:roster、page:detail，经同一宿主和管道执行；不依赖 CDP。页面记录的来源、字段及 partial 保存契约见[页面记录](page-observations.md)。只有 DOM 接口不可用时才检查下述保留的 API 路径，不能把缺 CDP 当作页面读取失败。

单独执行登录可以导入 client_host.mjs 并调用 createClientHost(browser)；登录本身没有读源依赖。后者是低层宿主，省略适配器不能用于采集，返回 SOURCE_ADAPTER_MISSING；生产 createRosterHost 已装配适配器，所选客户端没有必要能力时返回 CLIENT_SOURCE_UNAVAILABLE。这两个状态不能混淆。

先安装当前 Python 包，核对与所选源码一致，再调用 host.startTask，传 python、cwd、operation（sync/discover/login）、game（默认 all），可选 storageRoot、runtimeRoot、uid。宿主以参数数组启动 Python 私有工作进程，不使用 shell；协议不出现在可见终端。

默认在同一任务交叠两游戏；`parallelGames: false`（CLI 为 `--no-parallel-games`）用于串行兼容/对照。会话锁和浏览器占用仍唯一，各游戏来源内部串行；最多两条不同游戏请求在途。私有管道按递增请求 ID 接收、按对应 ID 返回，回复可乱序。控制/登录请求保持排他，并发恢复先收拢在途读源后统一登录和核验，再串行重做未提交游戏。共享两秒来源间隔同时由 Python 调度和并发宿主实际启动端约束。

## 分层与历史代码迁移

| 文件 | 职责 |
|---|---|
| client_runtime.mjs | 唯一装配入口，注入游戏读源插件及服务动作契约 |
| client_host.mjs | 活浏览器引用、登录问答、相关管道、取消和进程生命周期；不实现游戏请求或服务刷新 |
| client_network.mjs | 客户端公开能力、浏览器内请求与响应观察、原始文本、超时；不导入游戏 |
| client_page.mjs | 公开 DOM、加载与取消、官方角色面板和页面来源记录；不导入游戏 |
| games/*/page_source.mjs | 各游戏官方页面选择器、同步/拥有/当前值的判断 |
| games/*/page.py | 各游戏页面字段映射与保存流程调用 |
| page_source.py / page_snapshot.py | 页面来源协议核验、注入的游戏策略、两轮比较及独立原子存储 |
| games/genshin/client_source.mjs | 原神 Binding 与养成请求覆盖；移植 donor d2b70ac 的 OfficialCollector |
| games/zzz/client_source.mjs | 绝区零注册接口、DS、设备头准备与服务初始化回执；移植同一 donor 的 BrowserTransport/页面准备 |
| games/zzz/endpoints.py | 游戏服务初始化及一次局部刷新、名单闭合、详情与邦布的业务次序 |

两游戏不互相导入。公共层接受注入回调与契约；装配入口只选择插件，不核验游戏身份。登录只打开页面、提示本人登录并等待确认；服务准备属于游戏。没有恢复历史 Profile 驱动、短信自动化或认证材料导出。

## API 路径的能力与来源

API 读取器仅在所选标签页明确声明 cdp 能力时使用公开 CdpTabCapability.send/readEvents。没有声明时不访问该能力、不寻找调试地址、不切换浏览器后端，不把 DOM evaluate 当成网络接口。页面读取器使用只读 DOM 与正常 UI 操作，具有独立的 official-page 来源，不复用 API HTTP 来源证明。

原神监听实际 Network 请求、响应与完成事件，记录真实请求体、分页及来源，再通过 getResponseBody 读取原始 JSON 文本。绝区零通过同一标签页/会话中的浏览器原生请求调用注册接口；不导出 Cookie，不由 Node/Python HTTP 客户端重放。Origin/Referer 由浏览器决定，不伪造这两个头。SDK 拒绝或页面请求被阻断时明确失败，不换路线绕过。

设备头仅访问 donor 指定的少数 x-rpc 字段，留在当前 JavaScript 宿主内；不访问 Cookie/Authorization 值。用户确认一次新的登录后，宿主递增 generation，游戏插件丢弃上代设备头缓存并在后续读源时准备。初始化响应可能含认证材料，宿主只判断固定 HTTP/业务状态并丢弃响应体，经 service.prepare 返回 ready 与观察到的 origin；认证内容不进入 Python 管道。

业务 source.read 只返回 json、evidence、origin。json 保留原始文本，不进行 JavaScript 浮点解析再编码；Base64 先解码并严格检查 UTF-8。origin 为实际观察的 url、method、status、redirected，Python 再核验本游戏不可变端点。拒绝跳转、事件缺口、缓存/Service Worker 响应与非法来源；HTTP 429、风险状态停止账号任务。

| 游戏 | 来源 |
|---|---|
| 原神 | binding、cultivation-avatar-list |
| 绝区零 | binding、record:avatar/basic、record:avatar/info、record:buddy/info、cultivate:user/avatar_basic_list、cultivate:user/batch_avatar_detail_v2 |
| 绝区零服务能力 | cultivate:badge-login，经 service.prepare 调用，不作为业务 JSON 来源返回 |

原神 cultivation-avatar-list 的 evidence 必须含实际 request（uid、region、lang、空 element_attr_ids/weapon_cat_ids、page=1、正整数 size）及 pages_observed=[1]；其他页、过滤或身份变化即失败。绝区零每个身份首次使用养成服务前初始化一次；养成接口明确认证失效时允许服务内刷新一次，仍失败才进入账号恢复边界。其他 HTTP、风险或格式错误不触发局部重试。

各游戏 entry 先检查页面来源；可用则由页面路径读取官方角色面板身份并采集。DOM 不可用时，API discover 需要 Binding，API sync 需要全部本游戏 API 来源及服务能力。source.check 动态返回实际支持范围，缺项只终止该游戏并报告阶段，后续游戏继续。能力声明不替代身份和双轮校验。DOM 观察不能伪装为带 HTTP 证明的业务响应。

## 推进与登录交接

每个客户端工具回合内显式 await pumpWindow(task)。串行逐条推进；并发以十秒启动预算调度至多两条不同游戏读源，返回前收拢全部在途浏览器动作，登录问题立即交还。管道回调只入队，不在后台调用浏览器；每个来源步骤包括页面准备，总期限45秒。超时和取消中止后续动作，迟到结果被丢弃，不提交数据。CLIENT_TIMING 只记录请求序号、固定方法/游戏、队列等待与处理跨度，供区分排队和页面执行，不包含业务参数或结果。

客户端允许长前台调用时，在同一活跃调用内循环 await 多个 pumpWindow，避免每个十秒窗口返回模型造成调度空档。前台分段须以实际外层工具期限为准，预留在途来源收拢时间。当前实测桌面外层期限300秒，采用180秒启动预算、270秒执行超时；到预算后收拢并主动交还，下一调用立即继续同一任务。不同客户端单独核验预算；不能把执行移到工具调用外的后台循环。完整窗口、调用衔接和来源处理跨度分别记录。

pump 遇到登录确认即返回；host.pendingQuestion() 给出 attempt、问题及选项。AI 对新 attempt 主动询问本人是否完成官方页面登录，并保留问题和 attempt 的关联。调用 host.answerLogin("completed", question.attempt) 只沿用针对本次尝试的明确确认；尚未完成用 waiting，取消用 cancel。认证再次失效是新尝试，旧答复不适用。pendingQuestion 不含凭据。保留 host/task 和页面，不在 JavaScript 中阻塞等待人类答复。

确认后登录模块结束，继续 pump 到 task.status().done，由当前游戏核验身份并采集。等待期间页面关闭由下一次主动 pump 检测并终止。task.cancel() 或 host.cancel() 中止挂起调用、回收子进程；一秒内未退出则强制终止。进程关闭后释放浏览器占用，保留客户端浏览器。页面跨回合使用客户端保留标记。一次 host 只运行一个任务，新任务可继续借用同一个浏览器。

零尺寸页面可通过公开 viewport 能力暂时修复布局。正常 pump 结束调用恢复；工具异常或取消后，先 await task.completion，再主动 await host.releasePageSettings()，保留浏览器和登录态。

## 管道与扩展

请求包含 version=1、递增 id、method、session、params；首次 session.attach 的 session 为 null，之后每条请求与响应关联同一随机会话。响应固定为 version、id、session、result、error；成功 error=null，失败 result=null。帧为 16 MiB 内单行 UTF-8 JSON。错序、换会话、EOF、非法帧和取消均明确终止。

service.prepare 的 params 为 game、service、params，结果只含 ready=true 和 origin。服务到注册来源的映射由装配层通过 serviceSources 注入，公共宿主不硬编码游戏初始化流程。source.read 参数为 game、source、params、body；宿主向读源回调额外传实际 browser、AbortSignal、generation。

session.attach 可附带 page_reader=true，表示宿主装配了页面读取器；它不是全局 DOM 能力判定，各游戏仍独立 source.check。page 来源的 origin={kind:official-page,game,url}，evidence={surface,generation}。json 只编码观察到的 DOM 字段，不添加 API retcode、HTTP 状态或未观察到的实体 ID。API 来源保持原有三元结构与 HTTP 证明。

自定义客户端适配可在低层 createClientHost 注入 readSource、sourceNames、checkSources、prepareService、serviceSources。来源与服务契约会被复制，未知或重复来源被拒绝；checkSources 检查运行时能力，prepareService 只返回初始化回执。禁止以能力声明或空回调伪造实现。正常采集使用 createRosterHost。

worker 的 report/end 帧只承载固定状态、必要本机路径与退出码；原始来源和设备头不写公开日志。task.completion 在进程关闭后结算，须与协议最终结果匹配；未处理完待办时不能只等待 completion。最终 PASS 仍以游戏事务完成、验证和锁释放为前提。

## 验证边界

tests/test_client_sources.mjs 在浏览器事件边界提供合成输入，执行真正的请求构造、观察、读源插件、生产宿主、Python worker、双次采集及保存；另测精确文本、服务认证信息隔离、重登后缓存失效、事件缺口、跳转、能力拒绝与超时。tests/test_service_session.py 检查游戏服务初始化/刷新与账号登录解耦。该证据不能替代真实官方数据与当前客户端能力的验收；最新实测见[验证状态](release-validation.md)。

tests/test_client_pages.mjs 回归同一活浏览器、页面来源、稳定加载、取消和临时视口恢复；tests/test_page_workflow.py 用合成 DOM 记录运行实际页面来源协议、共享登录/过期恢复、两游戏两轮及原子保存。选择器和当前字段还需由真实内置浏览器验收，不能只依赖注入记录。
