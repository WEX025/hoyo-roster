# 手动图片验证码交接开发验证

> 历史归档：本文仅记录下述固定版本的实现和验证，不作为当前工作流或当前验收结论。按身份 Profile、发现后搬移、旧调度及旧人工登录步骤已被[当前四模块工作流](../workflow.md)取代。当前使用见[本机指南](../local-use.md)，历史测试数不代表最新源码已通过。

当前使用边界：**崩坏：星穹铁道暂时无法获取，请不要使用；原神目前不含圣遗物和完整属性面板。** 本文三游戏控制和 HSR 页面/guard 说明仅保留固定版本的合成技术证据，不推荐运行星铁。下方 worker 升级步骤只适用于当前可用的原神和绝区零；能力声明是用户产品决定，不推断上游原因，也不补称真实验收。

日期：2026-10-13。运行版本 `0.1.0rc1`；源码与测试 SHA：`345e097f5d028fc3c21ec1e1485c977fbc2059b1`。固定基线：`52bb6e177b30020b5d313cf2e23996fa2840944d`。本轮在新的本地隔离 checkout 开发；保留原工作区。只修改 hoyo-roster，共享登录协调/UI、进度、CLI 帮助、项目规则与相关合成测试；`CLAUDE.md` 仍为 `@AGENTS.md`。未修改游戏请求契约、请求白名单、存储格式或打包配置。后续交付文档提交不改变上述源码/测试字节。

## 行为与边界

- 登录入口等待和后续 Binding 轮询前，检查当前登录页及其 frames 的可见 Geetest 面板或图片/滑块操作提示。短信验证码字段、倒计时和隐藏面板不作为图片 CAPTCHA。
- 显示 `WAITING_FOR_MANUAL_CAPTCHA`，明确要求本人在本工具打开的所选游戏官方浏览器窗口点击图片或操作滑块，给固定官方页面标识；不输出动态 URL、UID 或凭据。AI 不选择图片、不解图、不绕过验证，不读取图片内容、答案或表单值。
- 保留同一页面和 Context，验证 UI 持续显示期间不发送自动 Binding 探测；本人仍可在官方页面操作。持续检测只提示一次；操控短暂超时保持等待，在原期限内恢复；新一轮验证重新提示。
- 面板消失显示 `CAPTCHA_CLEARED`，仅恢复既有发码/登录等待及精确 game/UID/region Binding 核验。取消验证面板也不证明验证通过；页面倒计时不证明短信实际送达；程序没有发码、重发、填写或认证点击步骤。错误账号、风控/限流仍停止。
- Ctrl-C 取消，阶段超时报 `LOGIN_TIMEOUT`，持续操控故障或登录窗口关闭报 `LOGIN_CONTROL_UNAVAILABLE`。沿用已有 finally/浏览器清理；HSR 临时登录页和 guard 被释放，持久 Profile 不删除。重跑仍先初始化会话并精确核验。
- 检测基于已知可见 DOM 标识/提示，不是对当前真实官方 UI 的完整性证明；未知 UI、官方契约变化或请求被原白名单拒绝需单独确认，不能扩大权限或代解验证。本改动不改变初始化阶段或 Binding 响应的风控停止策略。

## 可复现自测

平台：Darwin arm64 / Python 3.12.15。仅使用合成身份、页面、网络响应和测试替身；未启动真实浏览器，未读取真实 Profile/凭据，未发短信。依赖安装在隔离目录的 `.venv`；只安装公开 Python 包，没有安装/启动 Chromium。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir 'pytest==8.4.1' 'ruff==0.12.7' 'jsonschema==4.25.0' 'playwright==1.54.0' 'referencing==0.36.2' 'rfc3339-validator==0.1.4' 'simplejson==3.20.1'
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_manual_captcha.py tests/test_login_ux.py tests/test_login_flow.py tests/test_session_handoff.py tests/test_login_contract.py tests/test_game_chains.py tests/test_direct_entrypoint.py
.venv/bin/python -m ruff check src/hoyo_roster/login.py src/hoyo_roster/login_ui.py src/hoyo_roster/progress.py src/hoyo_roster/cli.py tests/test_login_flow.py tests/test_manual_captcha.py --select F
.venv/bin/python -m ruff format --check src/hoyo_roster/login.py src/hoyo_roster/login_ui.py src/hoyo_roster/progress.py src/hoyo_roster/cli.py tests/test_login_flow.py tests/test_manual_captcha.py
PYTHONPATH=src .venv/bin/python -m hoyo_roster --help
git diff --check
```

| 实际检查 | 结果 |
|---|---|
| 新增验证码定向测试（首次单独运行） | **PASS**：24 passed in 0.44s |
| 最终代码字节：上述七个登录/会话/契约/链路/CLI 测试文件 | **PASS**：205 passed in 141.71s；包含新增 24 项 |
| 修改 Python 文件的 Ruff F 检查 | **PASS**：All checks passed! |
| 修改 Python 文件的 Ruff 格式检查 | **PASS**：6 files already formatted |
| CLI `--help` | **PASS**：退出 0；包含本人点击、AI 不选择、保留会话、倒计时边界及等待上限 |
| `git diff --check` | **PASS**：退出 0，无输出 |
| 全仓测试/验证器、打包/安装资源检查 | **NOT_RUN**：本轮按授权只做定向与受影响回归，无打包/格式修改 |
| 独立审计、CI、PR 推送、合并、部署 | **NOT_RUN**：不在本轮开发自测范围 |
| 本改动真实验证码/短信/登录/绝区零完整流程 | **NOT_RUN**：由真实验收 worker 独立完成，本轮不接触其窗口、PTY、Profile 或环境 |

交付时的最新回传：真实验收 worker 在前置 `52bb6e1` 确认原神和绝区零身份、关闭后新进程认证复用、双次采集、保存、完整数据仓库/inspect PASS。这个结果只归属于前置固定版本；`345e097` 的真实 CAPTCHA 界面和重新真实登录仍为 NOT_RUN。用户明确该小改只需开发自测、免独审，已授权满足验证条件后合并；正式交付记录与实时 PR 状态见[当前交付状态](release-validation.md)。上表 CI/PR/合并的 NOT_RUN 是开发自测阶段事实，不是后续正式交付的永久禁止。

首次旧回归为 126 passed / 1 failed：新增等待抢先将已持续失控的入口超时误分类为 `LOGIN_TIMEOUT`。已将入口原有超时分类检查放在验证码检查之前，最终七文件回归覆盖该失败并通过。没有以放宽断言修复。

新增测试覆盖：入口前图片验证、frame 内验证、无 SDK 容器时的操作提示、短信/倒计时负例、三游戏重复验证期间请求计数不变、同会话恢复及错误身份/风控拒绝、原业务数据不变、超时、窗口关闭、取消、持续/短暂操控超时与不重复提示。相关既有回归继续覆盖会话复用、新进程合成交接、精确身份与双次采集/保存；离线通过不等同真实账号验收。

## 交给真实验收 worker 的升级步骤

1. 先完成或主动结束当前真实人工验证阶段；由该 worker 确认本工具命令和对应浏览器已正常退出。不得在使用中的 Profile 上替换代码、重启浏览器、删除 Singleton 或锁文件。保留原 Profile 和已保存数据。
2. 只在 worker 自己的代码 checkout 读取本轮本地提交（路径和准确 SHA 见交付），例如 `git fetch <本轮隔离仓库路径> captcha-handoff`，核对 `FETCH_HEAD` 与交付 SHA 完全一致，并确认基线 `52bb6e177b30020b5d313cf2e23996fa2840944d`。保持现有 checkout；另建代码目录或在其获授权升级分支应用该提交，不覆盖其他工作。
3. 使用 worker 自己既有的 Python 3.12 虚拟环境从新代码目录重新安装 `python -m pip install .`，先检查 `hoyo-roster --help` 中的 CAPTCHA 规则；运行当前版本的既有本人游戏/UID/私有 storage-root 命令，保持同身份 Profile，不复制凭据或其他身份状态。
4. 图片验证由本人在提示指向的官方窗口操作；确认暂停提示不刷屏，等待期间不自动重发短信，面板消失后仍由本人完成登录并精确核验。记录固定源码 SHA、脱敏状态码和实际验收结果；实际短信送达只能由本人确认。
5. 本轮不自行执行上述升级。前置 `52bb6e1` 两游戏真实完整流程已由 worker 回传 PASS；本改动真实 UI 尚未验收，准确保留该边界。按用户授权，小改开发自测且免独审；正式交付在核对远端、隐私与无新阻塞后处理 PR2，不宣称最新 SHA 重新真实登录或三游戏全验收。
