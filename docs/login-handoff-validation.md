# 登录与采集衔接修复记录

当前使用边界：**崩坏：星穹铁道暂时无法获取，请不要使用；原神目前不含圣遗物和完整属性面板。** 本文三游戏实现与测试描述仅为固定版本历史技术记录，不推荐运行星铁登录/采集，不推断上游原因；当前使用建议见 [README](../README.md)。

此文件保留 `dd1cc76`/`70fcd2c` 时的合成开发记录和当时 NOT_RUN 边界。后续 `52bb6e1` 已修复置前控制/导航关闭分类，真实验收 worker 回传原神、绝区零的身份、关闭后新进程认证复用、双次采集、保存及完整数据仓库/inspect PASS；`345e097` 图片 CAPTCHA 小改为 205 项合成回归 PASS，未在该 SHA 重新真实登录或复验 CAPTCHA 界面。最新版本与交付边界见[当前交付状态](release-validation.md)，不将不同固定版本的结果混为同一次验收。

## 范围与原始失败事实

基线：`746d332b802b12db7ce61d02337b6ee707f623b8`。本轮实现 SHA：`dd1cc76c28298f6f46ef10e5c7e06716616fea80`；后续记录提交只修改文档。全部身份、页面、响应和持久状态测试均为合成数据；没有读取私人 Profile、Cookie/token 或浏览器数据库，没有执行真实登录、短信、扫码或采集。

保留原验收事实：本人官方手机号登录成功，Binding 确认原神 UID/区服；同一 Profile、游戏、UID 随后的新进程 headless 非交互同步在初始 Binding 报 `AUTHENTICATION_REQUIRED`，尚未发出角色请求。随后获授权的前台非交互同步同样失败，因此不能归因于 headless。没有已知风险控制、限流或错误账号记录；原始认证响应未保留，不能断言 Cookie 根因。异常落在旧 `login.py:321` 的认证停止分支。再次登录时，外部 Mac 浏览器操控出现 `timeoutReached` / `errAETimeout`，CLI 又在入口检测的 30 秒窗口后报 `LOGIN_METHOD_UNAVAILABLE`；尚未发送第二次短信。

## 代码证据与修复

- 基线协调器在任何官方页面初始化之前就发出初始 Binding；浏览器刚重开时没有页面会话就绪边界。现在三游戏先在同一持久 Context 打开临时官方页面，等待页面网络静止及已观察到的会话校验结束，关闭临时页，再发送初始 Binding。临时页 guard 只允许官方页面资源与已注册 `verifyCookieToken`，阻止短信、QR 创建、Binding 和游戏业务 fetch。限流、风险、无效会话校验及加载/操控失败立即停止。初始化本身永远不证明身份，精确游戏/UID/区服校验保持原约束；错误账号在前台和 headless 都立即停止。
- 原神/绝区零基线的 `finally` 中，Context 关闭异常会跳过 Playwright 停止；现在保证停止运行时仍被尝试。三游戏原来在持久 Context 关闭之前打印登录成功；现在先报告 Binding 结果，唯一 CLI 在领域调用（包含浏览器关闭）成功返回后才报告 `STATUS=PASS`。
- 原入口检测把“30 秒内未观察到”分类为“不支持”，且操控读取可能无限阻塞。现在使用配置的登录等待上限、单次有界可见性读取、可取消等待和暂时操控超时恢复。迟到入口可继续；未观察到报 `LOGIN_TIMEOUT`，持续操控超时/页面关闭/操控不可用报 `LOGIN_CONTROL_UNAVAILABLE`。不再据此断言登录方式不受支持。外部工具本身的 Mac 控制连接仍须真实验收。
- 新登录页面准备前，先完整说明二维码/手机号步骤、官方通信、本机保存及复用、协议与权限边界，一次取得允许和方式选择。非交互仅选择方式不等于允许，缺失 `--allow-login` 报 `LOGIN_CONSENT_REQUIRED`。程序没有点击协议、发送短信或填写凭据的步骤；未知新协议和额外权限须本人另行确认。有效精确会话直接复用。

这些是源码和合成控制流证明的通用缺陷与修复；它们尚未证明原真实账号失败的完整因果链。没有导出会话材料、延长凭据有效期、重写 Cookie、绕过认证或添加账号特例。官方公开静态会话契约见 [登录契约证据](login-contract.md)。

## 验证

平台：macOS arm64，Python 3.12.15。下列命令中的 `python` 是本轮只读开发运行时；执行源码时显式使用 `PYTHONPATH=src` 和 `PYTHONDONTWRITEBYTECODE=1`。pytest 使用各次独立 basetemp，未复用私人数据。

| 验证 | 实际参数与结果 |
|---|---|
| 新增衔接回归 | `python -m pytest -q --basetemp=/tmp/hoyo-task18-focused-after-close tests/test_session_handoff.py`：PASS，59 项；最后收紧 API 资源类型后另跑初始化 guard 13 项，全部 PASS |
| 最终全量 | `python -m pytest -q --basetemp=/tmp/hoyo-task18-complete-final`：PASS，361 passed / 10 skipped，624.94 秒；10 项跳过是原恢复测试中没有对应写入/清理阶段的参数组合 |
| 静态与格式 | `python -m ruff check src tests tools --select F` / `python -m ruff format --check src tests tools`：PASS，80 文件 |
| 空基线 | `python tools/validate_repository.py --require-empty-baseline`：PASS，20 JSON / 12 schemas / 0 Build |
| 验证器自检 | `python tools/validate_repository.py --self-test`：PASS，14 正例 / 81 反例拒绝 |
| 构建与资源 | `python -m build --no-isolation` / `python tools/verify_distribution.py`：PASS，wheel/sdist 的 88 项包资源逐字节一致 |
| 干净安装 | 新建两个 Python 3.12 venv，分别 `pip install dist/hoyo_roster-0.1.0rc1-py3-none-any.whl` 和 `pip install dist/hoyo_roster-0.1.0rc1.tar.gz`：PASS；安装模块位于各自 site-packages，88 项资源与源码相同，三游戏合成身份离线 init、两个 CLI 入口、`pip check` 均 PASS |
| 真实 Chromium 会话持久性、当前官方 SDK 初始化/allowlist 兼容性、真实账号登录后重开同步、短信/QR/真实入口与外部 Mac 控制连接 | NOT_RUN，由原验收会话后续安排 |
| 新独立审计 | NOT_RUN，由父端另建独立会话 |

`test_session_handoff.py` 用真实 CLI、Provider、协调器、精确 Binding、双次角色链和存储，替换 Chromium/页面/响应及时间等待；两个独立子进程只通过自建的合成状态标记模拟持久 Profile。覆盖 login 后新进程前台/headless sync；不把该标记测试称为 Chromium Cookie 持久性验证。另覆盖初始化顺序、过期/错误身份/风险/无效响应、受限初始化 guard、加载与操控错误、31 秒才出现的入口、取消、未知协议/额外权限边界提示、一次授权及关闭失败不报告成功。

早期局部回归出现旧断言与新行为不一致的失败，已更新断言并补充上述拒绝测试；一次早期全量运行被人为 SIGINT 中断，最终报 1 failed / 298 passed / 10 skipped，不用作通过证据。原始真实失败事实未改写。沙箱内公开依赖下载起初因 DNS 限制失败，在获执行环境批准的联网安装后完成干净验证。
