# hoyo-roster

先读 [README](README.md)、[四模块工作流](docs/workflow.md)、[本机使用](docs/local-use.md)。Python 3.12；采集装配入口为 src/hoyo_roster/client_runtime.mjs 的 createRosterHost(browser)，低层宿主为 client_host.mjs。所有操作系统统一借用当前 AI 客户端内置浏览器；Codex 使用 Codex 内置浏览器。没有可操作宿主或数据读取能力时报告明确错误，不自行启动浏览器。

- 四个业务模块：登录、原神、崩铁、绝区零。各游戏拥有身份核验、采集、标准化、校验和保存；公共桥接、锁、节流、JSON 与存储不是额外业务模块。游戏不得导入另一游戏实现；公共层只接受注入的游戏回调和契约。
- client_runtime.mjs 是装配入口，允许选择游戏读源插件；client_network.mjs 与低层宿主不导入游戏实现。使用现成 createRosterHost，不要求执行 AI 临时补 reader。移植读源器只使用实际声明的公开 CDP 能力，未声明时不访问该能力、不发现调试地址、不用 DOM evaluate 发请求。浏览器原生请求留在所选内置浏览器会话内，不导出认证材料、不由独立 HTTP 客户端替代。
- 默认直接原神→绝区零。游戏先通过同一客户端会话读取自身官方 Binding；明确认证失效才调用登录。登录模块只打开官方页面、提示用户自行登录并主动询问是否完成；只沿用本次登录尝试的明确确认，不重复询问；认证再次失效开启新尝试并重新确认。确认后立即返回同一会话与页面，保留浏览器。登录模块不核验身份、不抓数据、不写结果。
- 手机号、短信验证码、扫码、协议、图片验证均由用户在官方页面操作。AI 不索取或代填、不发短信、不代勾协议、不代提交，不读取输入值、Cookie/token 或浏览器数据库。客户端未提供能力时不以 HTTP、系统浏览器、隐藏状态或复制认证材料替代。
- 客户端宿主持有活浏览器对象；私有双向管道仅传相关请求、确切业务来源 JSON 和固定状态，浏览器引用不落盘。session.attach、login.open、login.confirm、source.check、source.read、service.prepare 必须关联同一会话与递增请求 ID；错序、换会话、EOF、取消均明确终止；等待登录时关闭页面由下一次主动 pump 检测并终止。仅浏览器 ID 不能构成连接。普通终端不运行 --client-stdio。
- 数据读取宿主必须来自客户端受支持接口。readSource 接收同一浏览器对象、AbortSignal 及已注册的游戏/来源；适配器须响应取消，在后续浏览器动作前检查 signal，返回原始 JSON 文本、来源 URL/方法/状态/跳转证明和必要的请求覆盖证据；不伪造数据，不凭空填充来源，不用浮点重编码精确数值。未配置适配器报 SOURCE_ADAPTER_MISSING；已接适配器但某来源不支持报 CLIENT_SOURCE_UNAVAILABLE。各游戏按操作检查自身来源，不以全局布尔值拦截其他游戏。能力取决于当次客户端与页面，应实际发现；实测记录见 docs/release-validation.md，不能把历史缺能力写成永久规则。
- 用户确认只结束登录模块。当前游戏随后核验官方 game/UID/region；本机文件、Cookie 存在和公开角色目录不是身份或完整名单证据。已发现身份在任务内固定，重登后调用已注册的游戏核验回调，账号变化停止任务。多个身份与后续游戏复用同一会话。
- 游戏服务初始化不是人工登录。绝区零拥有养成服务初始化与一次局部刷新；服务认证响应仅由 JavaScript 游戏插件判断固定状态并丢弃，service.prepare 只返回 ready/origin。设备头只取既定 x-rpc 字段，留在宿主，不访问 Cookie/Authorization 值；一次新登录确认递增 generation，游戏后续动作自行重建缓存。每个来源步骤含浏览器准备总计不超过 45 秒，主动 pump 每次最多一条请求；超时/取消不得继续浏览器动作。
- 采集中明确认证失效时，丢弃未提交两轮采集，最多重登并重做当前游戏一次；不重抓已完成游戏，不混合会话版本。普通单游戏来源、schema 或保存失败继续其他游戏并汇总失败；身份冲突、认证恢复失败、风控、限流、管道损坏停止账号任务。
- 每个游戏保留自己的双次业务及来源覆盖比较、Decimal、lifecycle、名单/Build bijection 和完整度判断，只加载本游戏契约。崩铁公开 login/discover/sync 返回 GAME_PAUSED；保留离线格式/维护，不推断上游原因。原神不含圣遗物和完整属性面板；partial 也须名单闭合。绝区零必须满足既有全部覆盖条件才 complete。
- 结果默认在可核验本项目仓库 data/；无源码安装回退系统用户数据根 results/，不依赖 cwd、不写 site-packages。--storage-root 只覆盖结果；--runtime-root 选择仓库外的私有锁和节流元数据，使用 client-runtime-v1，不保存登录态。绝不探索、读取、迁移或删除真实旧认证和结果目录；没有兼容登录或迁移分支。
- 保留存储所有权、身份、current 指针、候选与代际验证。提交前失败保留旧 current；替换后持久化未知保留全部可能引用代际；已提交清理失败独立报告待办。恢复不能按 UUID 盲删。请求和结果事务完成、锁实际释放后才能输出最终 PASS；共享浏览器继续保留。
- 平台差异集中 platform_io/windows_io。POSIX 私有权限，Windows SID/DACL、本地固定 NTFS、拒绝 reparse/junction；不跳过锁、刷新和权限失败，不宣称 Windows 目录 fsync 或断电等价。客户端规则与 OS 无关不代表已验证所有平台。
- 测试只用新临时范围与合成身份/来源，默认路径提供者同样指向临时根。保持游戏规范化、两轮、候选和提交真实运行；宿主/管道也须跨进程验证。检查 pytest、node --test tests/test_client_host.mjs tests/test_client_sources.mjs、tools/platform_smoke.py、Ruff F/format、validate_repository.py 及 --self-test；改包时另跑 build、verify_distribution 和干净安装。读源器还须用浏览器事件边界的合成输入验证，不能只注入处理后的 fixture。真实验收与合成分开报告。
- 凭据、原始响应、设备头、个人账号截图、UID 和昵称不输出到对话或日志；管道不在可见终端展示。只报告固定状态、脱敏数量和必要结果路径。正常同步不提交/上传个人结果；data/ 只跟踪 .gitkeep，提交前运行 tools/check_private_data.py。未经授权不发布、部署、合并或改 CI/Actions。

本文件为代理规则权威；CLAUDE.md 只指向本文件。skill 只提供执行入口，不复制整套规则。客户端进程重启保持、其他客户端及平台未验收不能写 PASS。
