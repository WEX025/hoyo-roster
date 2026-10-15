# 开发与反馈

先读 [AGENTS.md](AGENTS.md) 和[四模块工作流](docs/workflow.md)。Python 3.12 包保存数据与运行游戏逻辑，client_host.mjs 运行在 AI 客户端并借用其内置浏览器；不得新增独立登录浏览器或认证材料搬运。

    python -m pip install -e '.[dev]'
    python -m pytest -q
    node --test tests/test_client_host.mjs
    python tools/platform_smoke.py
    python -m ruff check src tests tools --select F
    python -m ruff format --check src tests tools
    python tools/validate_repository.py --require-empty-baseline
    python tools/validate_repository.py --self-test
    python tools/check_workflow.py
    python tools/check_private_data.py
    python -m build
    python tools/verify_distribution.py

Node 宿主跨进程测试使用项目虚拟环境中的 Python；跨平台时选择该平台实际虚拟环境解释器。合成身份、来源、结果根和 runtime 根均为新临时范围，测试不得读取本人旧认证/结果目录。

测试只替换客户端接口与官方来源，登录协调、身份发现、两轮比较、normalize、lifecycle、candidate、store 和管道应运行真实代码。删除旧流程测试时保留适用的文件系统、恢复、精确数值及存储故障回归，并为新交接状态补齐测试。覆盖启动失败后的重试、挂起读取/SDK 的取消与迟到结果、等待时关页、过期确认及恢复核验失败后停止后续游戏。check_workflow 仅检查列明的静态规则和链接，不替代行为回归，也不能单独证明无历史残留。

真实登录由用户在内置浏览器自行完成；AI 主动询问后继续。真实来源验收要求客户端提供受支持的完整读取能力。不能以合成来源、公开页面或用户确认代替真实业务数据证明；当前 Codex 读取限制见[客户端桥接](docs/client-bridge.md)。

只报告固定错误和脱敏步骤，不附账号、凭据、原始响应、个人截图或 JSON。data/ 只跟踪 .gitkeep；提交前检查索引路径。正常同步不上传个人结果，不新增发布、合并、部署、后台服务或 CI/Actions。

POSIX 与 Windows I/O、目录/权限及断电边界见[平台支持](docs/platform-support.md)。本项目使用 MIT；依赖版本和许可证见[第三方声明](THIRD_PARTY_NOTICES.md)。固定源码与工作区状态、实际命令、PASS/FAIL/NOT_RUN 必须分别记录。
