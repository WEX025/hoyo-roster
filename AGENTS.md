# hoyo-roster

Python 3.12 单包；唯一用户入口 `hoyo-roster` / `python -m hoyo_roster`。
先读 README.md、docs/local-use.md；数据格式见 docs/data-format.md，隐私见 docs/privacy.md。

- 游戏实现位于 src/hoyo_roster/games/{genshin,zzz,hsr}；contracts 的 schema、空基线、验证器是格式权威。共享层只负责登录协调、锁、节流、候选和存储。
- 明确的本人游戏/UID 请求只授权该身份本机采集。使用用户提供的 UID，不推断他人身份；必须精确验证官方 game/UID/region Binding。不同身份的 Profile、锁和节流保持独立。
- 官方页面登录由本人选择二维码/手机号/取消并操作。会话有效时复用；凭据、手机号、验证码、Cookie/token、设备头、raw response、HAR/trace/截图不得收集、打印或提交。
- 遇身份冲突、认证失效、风险控制、限流、schema/来源漂移应停止；不绕过验证、不自动第三次采集。保持两次业务及来源覆盖比较、Decimal、lifecycle、名单/Build bijection 和完整安装验证。
- 原神 partial；绝区零仅满足闭合证据后 complete；星铁 partial，owned roster 不可证实时可能只更新 Profile并保留旧角色值。不得为了成功放宽来源/完整度。
- 个人数据保持在源码之外，正常同步不提交/上传。存储提交前失败保留旧 current；replace 后持久化未知保留所有可能引用代际；已提交的清理失败报告待办。恢复核验所有权、身份、当前引用和验证，不能按 UUID 盲删。
- 开发使用合成身份，只替浏览器/网络。运行 `python -m pytest -q`、`python -m ruff check src tests tools --select F`、`python -m ruff format --check src tests tools`、`python tools/validate_repository.py --require-empty-baseline` 与 `--self-test`。打包修改另跑 `python -m build` 和干净安装资源检查。
- 报告固定 SHA、实际命令及 PASS/FAIL/NOT_RUN；离线成功不等于真实账号验收。公开 PR/Issue 不含个人路径或数据。未经授权不改可见性、部署、合并、CI/Actions或私有数据仓库。

本文件为项目代理规则权威。按 [OpenAI AGENTS 指南](https://developers.openai.com/codex/guides/agents-md/)保留短且可执行的项目规则；可选技能仅在 .agents/skills 中提供入口，遵循 [skills 建议](https://developers.openai.com/codex/skills/)并避免复制规则。Dot/App 专属工具的存在不保证普通 CLI 环境具有这些能力。
