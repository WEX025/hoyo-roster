# Chromium Profile 跨模式修复验证

基线：已合并 main `0c9956781532d2dbf3d651048fbeab05ffa456e0`。本轮只修原神和绝区零的持久 Context 启动参数，采集、Binding、候选、存储和平台 I/O 不变。

## 问题与修复

Playwright 1.54.0 的 `getExecutableName(options)` 在没有 channel 时，可见模式选 chromium，无头模式选 chromium-headless-shell。Mac 原生合成探针证明：相同 Profile 从可见 Chromium 切到默认 headless shell 后，一小时有效期的合成持久 Cookie 和原有会话 Cookie 都不能再读取；可见到可见正常。这个控制不涉及官方账号、认证材料或网络，也不依赖 Cookie 到期猜测。

原神和绝区零现显式选择 `channel="chromium"`，两种模式共用 Playwright 安装的完整 Chromium。官方文档说明 [chromium channel 选择新无头模式](https://playwright.dev/python/docs/browsers#chromium-new-headless-mode)；同时核对了本机安装的 1.54.0 driver 实现。没有伪造 User-Agent、导出/复制认证、修改 Cookie 期限、放宽风险或身份检查，也没有为失效会话造恢复。原神已有 restore-last-session 参数保持；绝区零原有会话期限语义保持。

## 实际验证

macOS arm64 / Python 3.12.15 / Playwright 1.54.0。使用临时合成 Profile 和合成 Cookie，脚本结束释放 Context/运行时并清理自己的临时目录。源码命令前置 `PYTHONPATH=src`；浏览器二进制复用已安装版本。

| 检查 | 结果 |
|---|---|
| 未改源码默认模式探针：headed→headed | PASS：合成持久与会话 Cookie 都保留 |
| 未改源码默认模式探针：headed→headless、headless→headed | 已复现失败：合成持久 Cookie 不可复用；作为负向控制，不计通过 |
| 固定完整 chromium 的原语探针，以上三个交接方向 | PASS：合成持久与会话 Cookie 均保留 |
| `python tools/browser_profile_smoke.py`，使用两游戏真实启动器 | PASS：两游戏 × 三交接方向，共 6 组；均保存/关闭/重启核对合成持久 Cookie，原神另核对其原有会话恢复语义 |
| `python -m pytest -q tests/test_session_handoff.py tests/test_login_flow.py tests/test_game_chains.py tests/test_platform_io.py -x` | PASS：144 passed；覆盖两游戏两模式的固定 channel、关闭顺序、登录/业务链、跨平台 mock |
| Ruff F 与 format check；`python tools/platform_smoke.py` | PASS；真实本机权限、跨进程锁、保存/恢复及 CLI |

这个原生探针证明本机 Profile 原语修复，不等于真实账号同步成功。真实扫码、身份核验、保存、重启复用的结果只在本机私有验收记录中记录，不能将历史成功或上述合成通过冒充真实采集通过。Windows 实机仍 NOT_RUN，新增 channel 为 Playwright 正式跨平台 API；mock 不替代本人 Windows 验收。没有 CI/Actions、独立审计 Agent、私有数据上传或认证迁移。
