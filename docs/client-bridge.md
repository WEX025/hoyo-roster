# 客户端浏览器桥接

宿主文件为 src/hoyo_roster/client_host.mjs，随 Python 包和源码分发。AI 在当前客户端 JavaScript 运行时导入 createClientHost，并传入已经通过客户端文档选定的实际 browser 对象。不得传一个浏览器 ID 或创建独立浏览器。

## 执行

先安装当前 Python 包，使用该环境解释器；调用 host.startTask，传 python、cwd、operation（sync/discover/login）、game（默认 all），可选 storageRoot、runtimeRoot、uid。该方法以参数数组启动 Python 私有工作进程，不使用 shell；标准输入输出均为管道，不向终端展示协议。

每个客户端工具回合内显式 await task.pump()，它只在当前回合处理待办浏览器动作；管道回调只入队，不在后台调用浏览器。pump 遇到登录确认即返回，不阻塞等待人类回答。host.pendingQuestion() 返回 attempt、问题及选项。AI 对新 attempt 使用用户输入工具主动询问是否完成官方页面登录，并保留 attempt 与问题的关联。回答“已完成”后调用 host.answerLogin("completed", question.attempt)；未完成用 waiting，取消用 cancel，均须传该 attempt。同一等待尝试不重复催问；仅可沿用针对本次尝试的明确确认。认证再次失效会生成新 attempt，旧确认不能用于新尝试。pendingQuestion 不包含凭据。不要在 JavaScript 中阻塞等待人类答复；保留 host/task 变量并将控制交回用户。

用户回答后继续调用 task.pump()，直到 task.status().done；不要在未处理待办请求时只等待 completion。task.status() 提供固定报告、待处理数量、是否结束、宿主 errorCode 和待答问题。下一次主动 pump 检查待答页面是否仍存在，关闭时终止等待；不会在后台调用浏览器。task.completion 在工作进程关闭后给出退出码、宿主 errorCode 及固定报告，正常成功须匹配协议最终结果。task.cancel() 或 host.cancel() 中断所有待办和挂起 SDK/来源等待，并终止工作进程；一秒内未退出则强制终止。进程关闭后释放浏览器占用，不等待挂起适配器返回，迟到结果不再写入管道或结果。保留浏览器。页面需按客户端保留机制维持到后续回合。一次 host 只用于一个任务；新任务可创建新的 host，仍传同一个客户端浏览器对象。

## 数据适配

createClientHost(browser) 默认没有完整数据源读取能力；同步/发现明确返回 CLIENT_SOURCE_UNAVAILABLE，登录仍可执行。

只有实际存在受支持的客户端读取接口时，才传 readSource 函数。它收到实际 browser、signal（AbortSignal）、game、source、params、body；适配器须响应 signal 中止，在每次后续浏览器动作前检查取消；宿主无法撤回适配器已执行的外部动作，但会丢弃终止后的结果。返回且只返回三个字段：json 是客户端直接读取的原始官方 JSON 文本，evidence 为必要的来源覆盖证据；origin 为来源 url、method、status、redirected 的观察值。Python 精确核验注册端点/方法、拒绝跳转和非成功状态，HTTP 429 按限流停止。不要先用 JavaScript JSON.parse 再 stringify 重编码来源数字，不从内存中的私有应用状态或认证目录取数据，不通过独立 HTTP 或 Cookie 导出补齐能力。

允许来源：

| 游戏 | source |
|---|---|
| 原神 | binding、cultivation-avatar-list |
| 绝区零 | binding、record:avatar/basic、record:avatar/info、record:buddy/info、cultivate:user/avatar_basic_list、cultivate:user/batch_avatar_detail_v2 |

来源字段和身份参数以各游戏 endpoint_specs、source/transport 及规范化代码为准。原神 cultivation-avatar-list 的 evidence 必须包含官方观察到的 request（uid、region、lang、空 element_attr_ids/weapon_cat_ids、page=1、正整数 size）及 pages_observed=[1]。缺失证据、过滤条件非空、未知多页不能保存。

宿主可报告固定的认证失效、风控、限流、页面不可用、数据能力不可用等错误，不能把响应体或工具异常文本当公开错误。数据源不得记录或返回 Cookie/token、手机号、验证码、设备头等认证材料。

当前 Codex 仅有 DOM/页面操作及页面声明的 WebMCP；原神和绝区零当前官方页面未声明数据工具。因此没有可提供给 readSource 的完整受支持数据接口。这个限制是当前运行结果的一部分，不能把测试用 reader 当成可用的真实适配。

## 管道契约

请求包含 version=1、递增 id、method、session 和 params。首次 session.attach 的 session 为 null；宿主返回随机会话关联值。后续请求与响应均匹配该值和 id。响应固定为 version、id、session、result、error；成功 error=null，错误 result=null。协议数据限制为 16 MiB，使用单行 UTF-8 JSON。

Python worker 的 report/end 帧承载固定状态、必要本机结果路径及退出码；数据源帧只在进程私有管道中交换，不写源响应日志。Python 在 login 返回后进入当前游戏，直接通过同一管道请求来源，不存在另一个浏览器接收步骤。工作进程结束不关闭客户端浏览器。

## 验证

Python 回归使用合成源驱动真实登录模块、游戏、两轮、规范化、候选及存储。Node 回归使用真实子进程和管道，检查同一 browser 对象进入来源读取、一次用户确认、两游戏采集保存，以及页面关闭和换会话拒绝。来源是合成的，不能替代真实官方数据验收。
