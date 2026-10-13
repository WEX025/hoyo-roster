# 开发与反馈

先读 AGENTS.md。项目是 Python 3.12 单包、单用户命令；游戏源码在 `src/hoyo_roster/games/{genshin,zzz,hsr}`，共享登录/存储层只协调，不把不同游戏或身份混成同一 authority。contracts 内的 schema、空基线和验证器随 wheel/sdist 分发。

当前产品边界：崩坏：星穹铁道暂时无法获取，请不要使用；保留实现/格式仅供开发和既有数据解释。原神目前不含圣遗物和完整属性面板，名单仍须闭合；绝区零已支持能力保持。技术模块、合成测试或历史安装验证存在不构成星铁可用承诺，不推断上游原因。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check src tests tools --select F
python -m ruff format --check src tests tools
python tools/validate_repository.py --require-empty-baseline
python tools/validate_repository.py --self-test
python -m build
```

合成回归只替换浏览器、网络及其时间等待，业务 Provider、协调器、双次采集、normalize、lifecycle、candidate 和 store 应运行真实代码。增加关键失败/恢复测试；不要以单个函数 stub 的成功证明完整链路。资源验证须从干净 wheel/sdist 安装运行，包含三游戏模块、schema 和验证器。

修改官方请求契约时引用官方公开静态源码并保留 URL/哈希/日期；静态证据与真实账号验收分开。官方验证码、短信、二维码等认证操作由本人完成，真实账号测试需明确授权。真实未执行记录 NOT_RUN，不能把离线通过称为发布验收通过。

报告注明固定源码 SHA、实际命令、通过/失败/未执行和平台。PR 写清问题、行为变化和验证；不要附个人路径、真实账号、凭据或响应。提交问题时提供版本、操作系统/Python版本、固定错误代码、脱敏步骤及预期行为；完整数据和浏览器状态无需上传。发现隐私漏洞时先使用仓库所有者可用的私密联系渠道，不在公开 Issue 放入秘密。

本项目采用 MIT；新增依赖先核实实际版本许可证并更新 THIRD_PARTY_NOTICES，不引入未授权游戏素材。不添加 CI/Actions、部署、合并或后台服务作为常规修改的一部分。
