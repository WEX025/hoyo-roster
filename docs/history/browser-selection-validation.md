# 系统浏览器选择验证

> 历史归档：本文仅记录下述固定版本的实现和验证，不作为当前工作流或当前验收结论。按身份 Profile、发现后搬移、旧调度及旧人工登录步骤已被[当前四模块工作流](../workflow.md)取代。当前使用见[本机指南](../local-use.md)，历史测试数不代表最新源码已通过。

2026-10-14，基于本机 Windows 修复提交 `e0d06b896b83cb2657f243c4bd1fa3fa77a54ccd`。本次验证对象为共享 `browser_runtime`、原神/绝区零启动链、发现后的 Profile 放置、退出登录保护及安装说明；最终提交 SHA 记录在本机交付报告中。

## 行为

- 新会话：Windows 优先已安装的 Edge，macOS 优先已安装的 Chrome，其他平台使用 Playwright Chromium。不跟随默认浏览器，不使用系统 Safari，不自动安装系统浏览器。
- 首选浏览器缺失或首次启动抛出 Playwright 启动错误时，接触官方页面前尝试 Chromium；两种浏览器分别使用自己的私有子目录。Chromium 缺失时停止并提示同环境安装命令。
- 首次启动前原子保存 channel；后续可见/无界面运行使用保存的选择。已有会话启动失败、取消、非预期异常、元数据失败及业务阶段的登录/身份/风控错误不重新选浏览器。
- `browser-choice.json` 和子目录随本次新建且确认关闭的发现 Profile 一起放入空身份目标。发现及退出登录检查所有支持 channel 的 Singleton 标识，不删除活跃或残留标识。
- 不迁移旧 Profile。无选择标记的非空目录、损坏标记或缺失选定子目录保留并停止；本人可显式选择新认证根并首次登录。

## 合成与原生验证

Windows 11 x64、Python 3.12.14、Playwright 1.54.0；系统 Edge `155.0.4283.45`，Playwright Chromium `139.0.7258.5`。开发检查设置 `PYTHONPATH=src`；pytest 的 basetemp 显式位于本次工作目录的仓库外新建范围，私有目录检查保持启用。

| 检查 | 实际结果 |
|---|---|
| `tests/test_browser_runtime.py` | 39 PASS：平台选择、分目录启动回退、无需安装 Chromium 即可启动系统浏览器、备用缺失/安装后复用、跨模式固定选择、旧目录/坏标记/重复 JSON 键拒绝、元数据失败先停止、嵌套 Singleton 保护、发现后保持选择、两游戏业务失败关闭且不换浏览器、CLI 固定提示 |
| session_handoff / discovery / direct_entrypoint / game_chains | 150 PASS；与最初 27 条浏览器选择测试合跑共 177 PASS，新增浏览器检查后再单跑最新 39 PASS；总计 189 条不同检查通过 |
| `tools/browser_profile_smoke.py` | Windows 原生 Edge PASS：两个游戏各可见→可见、可见→无界面、无界面→可见，持久合成 Cookie/localStorage，原神合成会话 Cookie，以及发现后目录放置复用 |
| 同脚本 `--channel chromium` | Windows 原生 Chromium PASS：同上全部检查；强制 channel 仅用于开发探针，不是公开 CLI 的会话切换入口 |
| `tools/platform_smoke.py` | Windows 原生权限、跨进程锁、保存/恢复、身份 key、CLI PASS；普通用户目录 fsync 等价不存在，断电验收 NOT_RUN |
| Ruff F / format | PASS |
| 空基线完整验证 / 验证器自测 | PASS；14 正例、81 反例 |

原生浏览器探针只在本次新建临时目录使用虚构页面和合成状态，测试页面请求全部拦截为合成响应；没有官方登录或角色采集，不读取/导出真实认证材料。成功后仅清理该新建范围，不确定失败保留。首次测试误用了仓库内临时路径，被真实认证根保护拒绝；改为仓库外 basetemp 后通过，未放宽保护。

macOS 系统 Chrome、Linux 及系统浏览器更新后的兼容性实机验证 NOT_RUN。新 Edge 路径下的真实扫码、精确 Binding、双次采集与保存/重启复用 NOT_RUN；此前 Chromium 的真实账号验收不能代替 Edge 的真实验收。启动回退的失败注入是合成证据，不能称为实际系统浏览器损坏验收。未执行完整仓库 pytest、CI/Actions、推送、PR 更新或合并。

Playwright 对已安装 Edge/Chrome 的支持、企业策略限制及 WebKit/Safari 区别见[官方浏览器说明](https://playwright.dev/python/docs/browsers)。
