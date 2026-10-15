# 辅助短信验证码输入验证

基线：main `d2b70ac6f3c55dce1bf3dc088a0e66f449845995`。用户要求提供手机号后主动询问本次验证码，由工具代填同一官方网页。旧的“短信验证码必须本人在网页输入”规则已同步修改，图片 CAPTCHA 仍本人操作。

## 行为

辅助手机号动作发送一次后，只有严格 `SMS_ACCEPTED` 与唯一可见官方表单核验通过才输出 `OTP_INPUT_REQUIRED`。助手在这个状态主动询问用户；CLI 以隐藏 stdin 接收 6 位 ASCII 数字，临时填写一次，下一轮重新核验 CAPTCHA、回执、协议与控件后提交一次“登录”。输入值不打印、不落盘、不写命令行、计时或截图。聊天回答仍属聊天内容。`OTP_FILLED` / `LOGIN_SUBMITTING` 不是认证成功，精确 Binding 和完整采集/关闭仍是最终依据。

等待输入用可取消任务，不阻塞既有登录轮询；终止时清理任务及恢复终端回显。冷却/未知回执不询问历史验证码，风控/限流继续停止。未知控件、填写/点击不确定交还同一窗口，不重试或重新发码。新的流程跟随原有辅助手机号授权；未启用辅助输入的流程仍可本人网页操作。

## 实测（macOS arm64 / Python 3.12.15 / Playwright 1.54.0）

- 登录/手机号/隐藏终端/回执/CAPTCHA/会话交接/既有审查回归：289 passed, 1 skipped（原生测试选择开关未启用）。命令：`PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_otp_login.py tests/test_phone_login.py tests/test_login_ux.py tests/test_login_flow.py tests/test_session_handoff.py tests/test_terminal.py tests/test_sms_receipt.py tests/test_manual_captcha.py tests/test_audit_regressions.py`。
- 后续补充两个游戏的真实 coordinator 接入合成回归：`tests/test_otp_login.py` 共 22 passed（含上述已覆盖的 20 项）。确认 OTP 动作触发、精确 Binding 成功、一次提交、监听与输入任务清理。
- Chrome 原生合成 DOM：17 passed，包含原神/绝区零新的发码→隐藏输入→填入→提交一次流程。命令：`HOYO_NATIVE_LOGIN_SMOKE=1 HOYO_TEST_BROWSER_CHANNEL=chrome PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_login_dom.py`。使用新 Context、拦截所有请求，无真实短信或个人会话。
- Ruff F、全目录 format check、空基线验证、validator self-test、Git 索引隐私检查：PASS。

官方公开前端源码证据见 [login-contract.md](login-contract.md)，不包含真实验证码或私有响应。当前变更的真实短信验证码端到端、真实账号采集、Windows/Linux 实机：NOT_RUN。后续真实流程应另行记录，不得用合成验证代替。

## 普通登录控件适配简化（2026-10-15）

按用户确认的自动手机号/验证码工作流，移除整段协议文字逐字相等、原生 checkbox 必须可见及全页面只能一个 SDK frame 的要求。只匹配相关可见表单，验证码输入定位不再同时依赖 type、maxlength、placeholder 三个展示属性；仍要求本次输入 6 位 ASCII 数字。已授权协议支持展示文字、空白/标点、二维码缺少自动注册说明、hideBack 参数的合理差异；同一自定义复选框使用可见 SDK 指示控件勾选并读回状态。

普通控件暂缺、按钮暂不可用先自动等待最多 15 秒，不立即要求用户手动填号/验证码或重复授权。固定 CONTROL_STAGE/CONTROL_REASON 区分手机号控件、协议容器、协议链接/名称、额外文本及按钮状态，不输出页面文字或输入值。未知结构持续不恢复、实际副作用结果不确定仍明确停下，官方 Binding、来源闭合、双采集、风控/限流、一次发码及敏感输入保护未移除。

最终定向回归 **298 passed, 1 skipped**；Chrome 全拦截合成 DOM **18 passed**，新增隐藏原生 checkbox + 可见样式控件、协议区延迟挂载、文案/展示参数差异、多个 SDK frame 和验证码表单延迟就绪。Ruff F、format check、Git diff check、空基线验证、validator self-test、索引隐私检查均 PASS。本轮真实页面适配与真实短信端到端仍需单独记录，不能以合成测试代替。
