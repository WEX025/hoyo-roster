# hoyo-roster

先读 [README](README.md)、[四模块工作流](docs/workflow.md)、[本机使用](docs/local-use.md)。Python 3.12；采集装配入口为 src/hoyo_roster/client_runtime.mjs 的 createRosterHost(browser)，低层宿主为 client_host.mjs。所有操作系统统一借用当前 AI 客户端内置浏览器；Codex 使用 Codex 内置浏览器。没有可操作宿主或数据读取能力时报告明确错误，不自行启动浏览器。

完整原神+绝区零页面同步优先使用 src/hoyo_roster/client_workflow.mjs 的 openCollectionWorkflow；顶层基础设施负责阶段、配置、版本、计时、验证与报告，不新增业务模块。按返回的 nextAction 继续同一个 advance，登录为显式等待；不临时拼装命令或重复启动任务。进程恢复只复用同版本、哈希和格式有效的已提交快照，并重新读取官方身份；未提交游戏重做双轮。活控制器/准备进程/worker 不接管、不盲杀；恢复元数据不得保存认证材料、浏览器对象或登录确认。完整入口须随 wheel 分发。详见[持久化工作流](docs/timed-collection.md)。

- 四个业务模块：登录、原神、崩铁、绝区零。各游戏拥有身份核验、采集、标准化、校验和保存；公共桥接、锁、节流、JSON 与存储不是额外业务模块。游戏不得导入另一游戏实现；公共层只接受注入的游戏回调和契约。
- client_runtime.mjs 是装配入口，注入各游戏页面/API 读源插件；client_page.mjs、client_network.mjs 与低层宿主不导入游戏实现。使用现成 createRosterHost，不要求执行 AI 临时补 reader。默认优先公开 DOM 读取；API 路径仅使用实际声明的 CDP 能力。未声明时不访问 CDP、不发现调试地址、不用 DOM evaluate 发请求。浏览器原生请求留在所选内置浏览器会话内，不导出认证材料、不由独立 HTTP 客户端替代。
- 默认在同一任务交叠原神与绝区零；显式 parallelGames:false/--no-parallel-games 可切换串行。每游戏内部串行，共享会话锁和两秒来源间隔；并发还在宿主实际启动端限制间隔，不靠两个独立任务绕开锁。游戏先通过同一客户端会话读取自身官方身份：页面路径读取官方角色切换面板的 UID、区服显示名和昵称；API 路径读取 Binding。明确认证失效才调用登录。登录模块只打开官方页面、提示用户自行登录并主动询问是否完成；只沿用本次登录尝试的明确确认，不重复询问；认证再次失效开启新尝试并重新确认。确认后立即返回同一会话与页面，保留浏览器。登录模块不核验身份、不抓数据、不写结果。
- 手机号、短信验证码、扫码、协议、图片验证均由用户在官方页面操作。AI 不索取或代填、不发短信、不代勾协议、不代提交，不读取输入值、Cookie/token 或浏览器数据库。客户端未提供能力时不以 HTTP、系统浏览器、隐藏状态或复制认证材料替代。
- 客户端宿主持有活浏览器对象；私有双向管道仅传相关请求、确切业务来源 JSON 和固定状态，浏览器引用不落盘。session.attach、login.open、login.confirm、source.check、source.read、service.prepare 必须关联同一会话与递增请求 ID；错序、换会话、EOF、取消均明确终止；等待登录时关闭页面由下一次主动 pump 检测并终止。仅浏览器 ID 不能构成连接。普通终端不运行 --client-stdio。
- 数据读取宿主必须来自客户端受支持接口。readSource 接收同一浏览器对象、AbortSignal 及已注册来源；适配器须响应取消，在后续浏览器动作前检查 signal。API 来源返回原始 JSON 与实际 HTTP 证明；页面来源返回 DOM 记录与 kind=official-page、页面 URL、game、surface、generation，不虚构 HTTP 状态或分页。DOM 只读可见页面承载的信息，不读应用隐藏状态或登录输入值。未配置适配器报 SOURCE_ADAPTER_MISSING；来源不支持报 CLIENT_SOURCE_UNAVAILABLE。各游戏独立检查自身来源，缺 CDP 不拦截页面读取。详见 docs/page-observations.md。
- 用户确认只结束登录模块。当前游戏随后核验官方 game/UID/region；本机文件、Cookie 存在和公开角色目录不是身份或完整名单证据。已发现身份在任务内固定，重登后调用已注册的游戏核验回调，账号变化停止任务。多个身份与后续游戏复用同一会话。
- API 路径的游戏服务初始化不是人工登录。绝区零拥有养成服务初始化与一次局部刷新；服务认证响应仅由 JavaScript 游戏插件判断固定状态并丢弃，service.prepare 只返回 ready/origin。设备头只取既定 x-rpc 字段，留在宿主，不访问 Cookie/Authorization 值；新登录确认递增 generation，游戏自行重建缓存。页面路径通过官方 UI 自身加载服务，不代发初始化请求。每个来源含准备总计 45 秒；并发模式最多两条不同游戏来源在途，同游戏和登录控制不重叠。主动窗口至多十秒启动预算，再收拢全部在途来源，不在工具调用外继续浏览器工作；登录确认挂起除外。超时/取消不得继续业务浏览器动作，临时视口设置可在明确收尾时恢复。
- 采集中明确认证失效时，丢弃未提交两轮采集，最多重登并重做当前游戏一次；不重抓已完成游戏，不混合会话版本。并发失效先取消未提交游戏的 Python 轮次并收拢私有管道在途读源，再统一登录、复查全部固定身份，转串行重做未提交游戏；同一恢复不复用到后续再次失效。普通单游戏来源、schema 或保存失败继续其他游戏并汇总失败；身份冲突、认证恢复失败、风控、限流、管道损坏停止账号任务。
- 页面路径两轮读取已呈现的拥有名单和全部对应详情，前后核验身份，拒绝名单重复、缺详情和漂移。原神只取同步后的当前等级、三项技能、当前武器，排除右侧目标值；实际观察到官方“奇偶暂不支持计算”提示时保留该拥有行及 unsupported 原因，不伪造详情或跳过普通失败。绝区零只取明确当前的音擎、技能和驱动盘状态，排除未拥有与推荐。页面结果固定 partial、scope=rendered-owned-characters，名称仅为匹配标签，不猜数字 ID，不声称服务端名单闭合或完整邦布。页面当前只支持面板唯一角色，歧义停止。API 保留双次来源覆盖、Decimal、lifecycle 和名单/Build 闭合；绝区零 API 满足原有全部条件才 complete。崩铁采集返回 GAME_PAUSED。
- 结果默认在可核验本项目仓库 data/；无源码安装回退系统用户数据根 results/，不依赖 cwd、不写 site-packages。页面记录在 page-observations-v1 按游戏/UID 哈希隔离，校验后原子替换 snapshot.json；不覆盖 game-results-v3 的 API 代际。页面保存拒绝名单缩小和角色等级回退，相同数据不重写；替换后持久化失败保留现存文件并报告未知，不删除。--storage-root 只覆盖结果；--runtime-root 选择仓库外 client-runtime-v1 私有锁/节流元数据，不保存登录态。绝不探索、迁移或删除真实旧认证和结果目录。
- 保留页成为零尺寸时，可在同一个客户端浏览器中新建游戏页，复用登录并重新核验身份，保留原页；不复制认证。新页仍零尺寸时才临时使用公开 viewport 恢复布局，不作截图美化。任务 pump 收尾恢复临时设置；取消或工具异常后，await task.completion，再主动 await host.releasePageSettings()。不关闭共享浏览器。
- 保留存储所有权、身份、current 指针、候选与代际验证。提交前失败保留旧 current；替换后持久化未知保留全部可能引用代际；已提交清理失败独立报告待办。恢复不能按 UUID 盲删。请求和结果事务完成、锁实际释放后才能输出最终 PASS；共享浏览器继续保留。
- 平台差异集中 platform_io/windows_io。POSIX 私有权限，Windows SID/DACL、本地固定 NTFS、拒绝 reparse/junction；不跳过锁、刷新和权限失败，不宣称 Windows 目录 fsync 或断电等价。客户端规则与 OS 无关不代表已验证所有平台。
- 测试只用新临时范围与合成身份/来源，默认路径提供者同样指向临时根。保持游戏规范化、两轮和真实提交；宿主/管道须跨进程验证。检查 pytest、node --test tests/test_client_*.mjs、tools/platform_smoke.py、Ruff F/format、validate_repository.py 及 --self-test；改包另跑 build、verify_distribution 和干净安装。API 事件边界、页面来源协议与共享登录恢复分别回归；页面选择器还须以当前官方页面真实执行验证。真实验收与合成分开报告。
- 凭据、原始响应、设备头、个人账号截图、UID 和昵称不输出到对话或日志；管道不在可见终端展示。只报告固定状态、脱敏数量和必要结果路径。正常同步不提交/上传个人结果；data/ 只跟踪 .gitkeep，提交前运行 tools/check_private_data.py。未经授权不发布、部署、合并或改 CI/Actions。

本文件为代理规则权威；CLAUDE.md 只指向本文件。skill 只提供执行入口，不复制整套规则。客户端进程重启保持、其他客户端及平台未验收不能写 PASS。
