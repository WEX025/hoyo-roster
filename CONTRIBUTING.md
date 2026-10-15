# 开发与反馈

先读 AGENTS.md。项目是 Python 3.12 单包、单用户命令；游戏源码在 `src/hoyo_roster/games/{genshin,zzz,hsr}`，共享登录/存储层只协调，不把不同游戏或身份混成同一 authority。contracts 内的 schema、空基线和验证器随 wheel/sdist 分发。

当前产品边界：崩坏：星穹铁道暂时无法获取，请不要使用；保留实现/格式仅供开发和既有数据解释。原神目前不含圣遗物和完整属性面板，名单仍须闭合；绝区零已支持能力保持。技术模块、合成测试或历史安装验证存在不构成星铁可用承诺，不推断上游原因。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python tools/platform_smoke.py
# 按变更选择受影响的 tests/test_*.py
python -m pytest -q tests/test_platform_io.py
python -m ruff check src tests tools --select F
python -m ruff format --check src tests tools
python tools/validate_repository.py --require-empty-baseline
python tools/validate_repository.py --self-test
python tools/check_private_data.py
python -m build
python tools/verify_distribution.py
```

禁止提交个人结果。源码 `data/` 只允许跟踪 `data/.gitkeep`，个人结果由 `.gitignore` 忽略；认证目录必须在仓库外。提交前运行 `python tools/check_private_data.py`，它只检查 Git 索引路径、不读取私人内容，只要 data/ 内跟踪了其他文件就报错。检查失败时先保留本机文件，再从索引取消跟踪（例如 `git rm --cached -- PATH`），重新检查；已经上传的个人数据还须私下处理暴露与历史，不能认为后续忽略就能撤回。`.gitignore` 不保护已跟踪文件、强制 add 或整个目录打包。此检查是手动本地命令，不安装 hooks，也不新增 CI/workflow。

测试使用临时目录与合成身份，默认路径提供者/--auth-root 同样指向临时私有根，不能依赖导入时冻结的 HOME。按改动范围运行定向测试及受影响回归；只有影响扩大时才有根据地扩展，不以默认全仓测试替代边界判断。

合成回归只替换浏览器、网络及其时间等待，业务 Provider、协调器、双次采集、normalize、lifecycle、candidate 和 store 应运行真实代码。增加关键失败/恢复测试；不要以单个函数 stub 的成功证明完整链路。资源验证须从干净 wheel/sdist 安装运行，包含三游戏模块、schema 和验证器。

修改官方请求契约时引用官方公开静态源码并保留 URL/哈希/日期；静态证据与真实账号验收分开。官方验证码、短信、二维码等认证操作由本人完成，真实账号测试需明确授权。真实未执行记录 NOT_RUN，不能把离线通过称为发布验收通过。

报告注明固定源码 SHA、实际命令、通过/失败/未执行和平台。PR 写清问题、行为变化和验证；不要附个人路径、真实账号、凭据或响应。提交问题时提供版本、操作系统/Python版本、固定错误代码、脱敏步骤及预期行为；完整数据和浏览器状态无需上传。发现隐私漏洞时先使用仓库所有者可用的私密联系渠道，不在公开 Issue 放入秘密。

跨平台 I/O、Windows 本人验收及目录刷新边界见 [平台支持](docs/platform-support.md)。Windows 原语须实机验证；macOS mock 不能替代。历史合成回归保留其 POSIX 假设，不把全套 pytest 当成 Windows 平台通过；Windows 优先运行独立 native smoke。

本项目采用 MIT；新增依赖先核实实际版本许可证并更新 THIRD_PARTY_NOTICES，不引入未授权游戏素材。不添加 CI/Actions、部署、合并或后台服务作为常规修改的一部分。

本轮架构整理、引用清理和定向自测证据见 [架构验证记录](docs/architecture-validation.md)；Windows 实机边界仍见平台支持文档。

涉及浏览器可见/无头切换或 Profile 复用时，运行 `python tools/browser_profile_smoke.py` 检查该系统的默认浏览器选择（Windows Edge、macOS Chrome，缺失时 Chromium）；安装 Playwright Chromium 后另运行 `python tools/browser_profile_smoke.py --channel chromium` 检查备用路径。它们仅创建新临时合成 Profile，测试页面请求拦截为虚构响应，不登录、不访问官方服务，不读取真实认证。

登录流程修改的定向 Chromium 合成测试：`HOYO_NATIVE_LOGIN_SMOKE=1 python -m pytest -q tests/test_login_dom.py`。所有页面/请求均由测试拦截，未知请求直接阻止；不访问官方服务、不使用真实认证。回执、超时与精确身份回归见 `tests/test_sms_receipt.py`、`tests/test_login_ux.py`、`tests/test_manual_captcha.py` 和 `tests/test_session_handoff.py`。实际证据见 [登录流程验证记录](docs/login-workflow-validation.md)。

macOS 离线 smoke 使用经过所有权与固定目标核验的系统临时别名 `/var`、`/tmp` 的真实路径；不放宽结果/认证根的符号链接保护，自定义 TMPDIR 或别名下的重定向仍拒绝。终端与一次发码回归见 `tests/test_terminal.py`、`tests/test_phone_login.py`；原生 Ctrl-C 及完全拦截的 SDK DOM 检查可用 `HOYO_NATIVE_LOGIN_SMOKE=1`，它们不发真实短信。
