# 发布候选验证记录

## 2026-10-13 当前交付状态

运行版本仍为 `0.1.0rc1`。本次交付保留 PR2 既有历史；下面的 `b84d46c` 结果是首轮记录，不代表最新源码或当前验收状态。源码实现固定为 `345e097f5d028fc3c21ec1e1485c977fbc2059b1`，其直接前置为 `52bb6e177b30020b5d313cf2e23996fa2840944d`；之后的交付文档提交不改变 `src/`、`tests/` 或打包配置字节。

| 版本/范围 | 结果及证据边界 |
|---|---|
| `52bb6e1`：原神、绝区零本人真实流程 | **PASS，依据真实验收 worker 回传**：精确身份、关闭后新进程认证复用、双次采集、保存、完整数据仓库验证与 inspect。交付代理没有重复登录、读取个人数据或操作该 worker 的 Profile/PTY/浏览器。公开记录不含真实 UID、角色池、认证材料或私有日志。 |
| `52bb6e1`：旧登录审计 F1/P2 与 O1/P3 | 导航加载和窗口置前控制分开限时；页面关闭统一为 `LOGIN_CONTROL_UNAVAILABLE`。回归包含在下述 205 项中。原审计事实保留在 [PR2 审查记录](https://github.com/WEX025/hoyo-roster/pull/2#issuecomment-6134865950)。 |
| `345e097`：图片 CAPTCHA 交接与受影响回归 | **PASS：205 passed in 141.71s**，包含 24 项新增控制；Ruff F、格式、CLI help、diff 检查 PASS。Darwin arm64 / Python 3.12.15；全为合成页面/网络/身份。实际命令见[验证码开发验证](captcha-handoff-validation.md)。 |
| `345e097`：真实 CAPTCHA 界面、重新真实登录 | **NOT_RUN**；不能把前置版本的真实验收写成该 SHA 已重新真实登录，DOM 检测不证明所有当前官方验证 UI 都被识别。 |
| 星铁真实完整流程、全部扫码/短信入口、其他平台 | **NOT_RUN**；保持既有 partial/来源与平台限制，不声称三游戏全验收。 |
| 本次独立审计 | **NOT_RUN，用户明确要求此小改只自测、免独审**；不冒称最新 SHA 独审通过。已有存储审计关闭结论见[固定 `746d332` 的复审](https://github.com/WEX025/hoyo-roster/pull/2#issuecomment-6131148779)。 |

用户已授权验证完成后合并，并明确本次 CAPTCHA 小改自测即可。此次交付据前置两游戏真实 PASS、修复与定向回归 PASS 核对无新阻塞后处理 PR2；推送/评论/合并的实际状态以 [PR2](https://github.com/WEX025/hoyo-roster/pull/2) 读回为准。没有创建或运行 CI/Actions、部署、发布包、修改私有数据仓库或删除既有工作区。下方“仍需独审/三游戏真实验收”的措辞只描述首轮候选当时的要求，不能覆盖此次用户授权和上述准确范围。

## 首轮候选历史记录

## 固定源码与范围

开发与验证源码提交：`b84d46c511448a6a3b501552613d8ecc37465c21`（后续提交仅补充此验证记录）。版本 `0.1.0rc1`；平台 **macOS 27.0 arm64，Python 3.12.15**。本轮只承诺已验证的开发/安装平台，不声称 Linux/WSL/原生 Windows 已验收。

基线公开 main：`5f356e6376c996071a39576c8c469b1082e9b6e0`。接续 [draft PR1](https://github.com/WEX025/hoyo-roster/pull/1) head：`4ad97e388445ba281d6fd7f1e58270a141c1d10e`。新分支 `release/first-candidate` 保留 PR1 原历史和审查 trace。既有独立首审不被本轮开发者测试替代，仍需父端新独立发布审计。

## 结果

| 项目 | 实际命令/执行范围 | 结果 |
|---|---|---|
| PR1 基线回归 | 原四包安装后 `python -m pytest -q` | PASS：59 项 |
| 最终源码回归 | `.venv/bin/python -m pytest -q --tb=short` | PASS：112 项，145.00 秒 |
| wheel 干净环境回归 | 在独立复制的 tests 目录，`../wheel/bin/python -m pytest -q tests --tb=short` | PASS：112 项，168.30 秒 |
| sdist 干净环境回归 | 同一 tests 源码，`../sdist/bin/python -m pytest -q tests --tb=short` | PASS：112 项，168.30 秒 |
| 空源码基线 | `.venv/bin/python tools/validate_repository.py --require-empty-baseline` | PASS：20 JSON、12 schemas、0 Build |
| 完整验证器自检 | `.venv/bin/python tools/validate_repository.py --self-test` | PASS：14 正例、81 反例拒绝 |
| 静态检查 | `.venv/bin/ruff check src tests tools --select F` | PASS |
| 格式检查 | `.venv/bin/ruff format --check src tests tools` | PASS：75 文件 |
| 差异检查 | `git diff --check` / `git diff --cached --check` | PASS；上游许可证原字节通过 .gitattributes 保留 |
| 发行构建 | `.venv/bin/python -m build --no-isolation` | PASS：sdist，并从 sdist 构建 wheel |
| 包资源与通知 | `.venv/bin/python tools/verify_distribution.py` | PASS：88 包资源，逐文件字节/hash一致，含三游戏、12 schemas、验证器及自检、空基线和通知 |
| 干净 wheel 安装 | `python3.12 -m venv ../verification/wheel` 后 `../verification/wheel/bin/pip install --no-index --find-links ../verification/wheels dist/hoyo_roster-0.1.0rc1-py3-none-any.whl pytest==8.4.1` | PASS |
| 干净 sdist 安装 | 独立新 venv 先装相同依赖，再 `../verification/sdist/bin/pip install dist/hoyo_roster-0.1.0rc1.tar.gz` | PASS（构建隔离启用） |
| 安装后资源/init | 独立 smoke 脚本对照当前源码的所有包资源，分别运行三游戏 `--operation init`，assert installed module 属于 site-packages | PASS，两环境各3游戏；浏览器登录 NOT_RUN |
| 依赖一致性 | 两环境 `python -m pip check` | PASS：无破损依赖 |
| 双入口 | clean wheel 的 `hoyo-roster --help`；clean sdist 的 `python -m hoyo_roster --help` | PASS |
| 官方静态契约 | 原始公开快照 hash与模块路径复核 + `tests/test_login_contract.py` 实际 guard 预检 | PASS；[官方源码证据](login-contract.md) |
| 本人真实登录/短信/扫码 | 未执行 | **NOT_RUN** |
| 三游戏本人真实端到端 | 未执行 | **NOT_RUN** |
| 三游戏实际二维码/手机号入口 | 未执行真实浏览器验收 | **NOT_RUN** |
| 最终独立发布审计 | 由父端在开发完成后执行 | **NOT_RUN / 待执行** |

依赖由正常 pip 下载，wheel 环境安装使用预下载依赖轮子；未捆绑 Chromium。安装 smoke 对 macOS 临时目录先 resolve 为实际路径，避免把系统 `/var` 符号链接误当成允许的私有路径。此处所有 UID、设备值和账号响应都是合成测试数据。

## 关键复现与修复证据

原始首审复现脚本在 PR1 基线上重现：current.replace 后父目录 fsync 报错导致当前引用目标被删除；旧代际清理失败误报同步失败并留下下一轮阻断；四条当前官方登录路径全部 abort；users/game 中间层为 0755。历史审查脚本和报告保持原样，未改写其结果。

新源码的 `tests/test_storage_faults.py` 覆盖元数据文件 fsync、replace前、父目录 fsync、完整验证、代际文件 fsync、恢复日志写入、current.replace 前与后、旧代际整树/部分清理失败。断言旧 current 在提交前失败时保留；持久化未知保留所有可能引用代际；已持久化提交报告成功+清理待办；恢复核验身份、所有权、文件清单/哈希、当前引用、权限和完整验证。改动/无归属/错误身份的残留拒绝，不能按 UUID 任意删除。

`tests/test_login_flow.py` 和 `test_login_ux.py` 运行真实三游戏 Provider 与协调器，覆盖空基线、精确会话复用、登录失效、错误 UID/game/region、超时、取消、交互选择、非交互缺失显式方式和未观察到官方入口；无凭据输入/打印。`test_login_contract.py` 对四条精确官方 POST、业务阶段禁止认证 POST、伪造 host、非 HTTPS、错误端口/方法/路径、业务写入和无关请求做实际 guard 检查。

`test_direct_entrypoint.py` 的原神链路和 `test_game_chains.py` 的 ZZZ/HSR 链路只替换网络、浏览器/进程探测及其时间等待，运行真实 Provider→协调→页面请求守卫/观察→双次采集→normalize→lifecycle→candidate→完整存储。包括精确 Decimal 百分比、无变化、来源漂移、进度回退拒绝、HSR 有名单/仅Profile、HSR 未证明 owned roster 时保留旧角色数据。原神未知分页提示也明确拒绝。

本轮遇到的开发测试失败（模拟字段缺失、报告器导入、旧自检 fixture 的浮点格式、未知分页遗漏）已修复并包含在以上最终通过结果；无尚未解决的自动化测试失败。没有真实账号验收，也没有 CI/Actions、部署、合并或修改私有数据仓库。

## 仍需本人和独立审计完成

本人在有界面环境逐游戏检查精确 UID/区服、所选登录方式、官方风险提示、双次采集、正式数据和第二轮无变化；星铁应特别核实名单是否可证实，以及仅Profile时旧角色数据的保留。发生错误保留旧 current，按 local-use 的 inspect/recover 流程处理。待这组真实流程及新的独立审计完成后，才能决定是否通过首次发布验收。
