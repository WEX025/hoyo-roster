# 登录流程验证记录

> 历史归档：本文仅记录下述固定版本的实现和验证，不作为当前工作流或当前验收结论。按身份 Profile、发现后搬移、旧调度及旧人工登录步骤已被[当前四模块工作流](../workflow.md)取代。当前使用见[本机指南](../local-use.md)，历史测试数不代表最新源码已通过。

基线为已合并 main `0c9956781532d2dbf3d651048fbeab05ffa456e0`，同分支先前 Chromium channel 修复见 [Profile 验证](browser-profile-validation.md)。本轮不进行真实短信/二维码挑战，不读取或改动私有结果/认证；无 CI、独立审计 Agent 或权限配置修改。产品规则唯一来源仍是 AGENTS.md。

## 已确认问题与修复

旧入口等待仅看登录文字，不打开面板/切换方式，也不能识别二维码图片是否就绪；等待入口期间不会检查已经成功的 Binding。人扫码后面板消失可能被误判超时。各等待阶段重新计算 900 秒，状态通知晚于面板准备，难以及时定位卡点。程序现按官方页面定位普通控件，唯一可见时至多点击一次；二维码/手机号就绪使用明确 SDK DOM，等待入口优先核验精确身份。本人参与通知在导航前发出，入口、CAPTCHA 和后续核验共用预算；结构未知/操控暂不可用约 15 秒后说明人工接管原因。既有初始化和导航/前台阶段各最多 30 秒。

旧临时辅助脚本的一次点击没有可核验发码回执，不能证明官方接受请求或短信送达；它不是产品入口。新产品不自动发送/重试，不读输入值，只跨 SDK frame 被动观察实际 POST 和回执，严格区分接受新发码、已有倒计时、官方拒绝、网络/格式/回执超时。retcode=0 本身不证明新发码；`sent_new=false` 不报已新发。后台任务在取消/失败后排空，监听卸载。回执未知明确停止并按既有生命周期关闭，不保留一个无人管理的登录进程。隐藏 iframe 不算可见 CAPTCHA/入口；短暂 frame 脱离可在期限内恢复。

Chromium channel、登录入口、回执观察与桌面控制工具是不同层。外部电脑操控工具曾出现超时、多个同 bundle ID 的 Chromium 歧义、缺少活动控制状态；产品修改不能宣称修好了外部工具或系统授权。CLI 的 Playwright DOM 不依赖该桌面工具。人工 CAPTCHA、协议确认和官方额外权限仍由本人处理。

## 官方公开静态证据（2026-10-13）

仅无认证 GET 静态文件；无页面执行、私有响应或请求重放，未把上游文件加入仓库。

| 公开文件 | SHA-256 | 支持事实 |
| --- | --- | --- |
| [计算器 bundle](https://act.mihoyo.com/ys/event/calculator/bundle_6f358e00e7158fc4a7f6.js) | `7f723ef8e50856129db7717f9bd46574b4d13f173f2d62151c75a803806951a5` | CN 顶部头像的登录回调与 CSS；不能用公共角色参考目录证明账号名单 |
| [账户 SDK](https://webstatic.mihoyo.com/dora/biz/mihoyo-account-sdk/main.js) | `768c2a73b0fea20e1b17daf96f72806a81ae867b61efeaf0ffb400ba2c7bd604` | 官方 login-platform iframe 来源；Cookie 命名含 SDK 前缀，少数常见名称的数量不能证明真实认证丢失 |
| [桌面登录 chunk](https://user.mihoyo.com/login-platform/js/login.35d0ab6d.js) | `afe0e060f2d1b7f456f4f2d2453ad33648923920811c587e8e17db4b27453d97` | 模式 SVG、二维码 loaded/expired/scanned、手机号 input#mobile、发送前手机号/协议校验、sent_new/countdown |
| [共享 API chunk](https://user.mihoyo.com/login-platform/js/5823.9f588959.js) | `c516fce638a759b0ef44c1eaa326c74794204ac98ade239b8562c272a3d6430d` | 官方 POST createLoginCaptcha 路径和“短信登录”标签 |

静态定位只覆盖已识别的大陆 SDK 和页面。上游结构改变时人工接管并保持明确超时/失败，不能凭猜测切换身份、接受协议或扩大请求。

## 定向验证

macOS arm64 / Python 3.12.15 / Playwright 1.54.0；源码命令前置 `PYTHONPATH=src`，复用已安装 Chromium。定向测试使用临时合成身份/认证根；原生 DOM 测试用全新 Context，全部网络经测试 route 拦截并阻止未知请求；没有真实短信/二维码、用户输入、图片识别或个人数据。合成手机号仅为测试页数据，不发往任何服务。

- 登录方式/允许、精确 Binding、错误身份/风险、CAPTCHA 重现暂停、超时/取消、页面关闭与最终状态：定向 pytest。
- 严格短信回执、非官方源、畸形/缺字段/布尔整数混淆、未知回执、拒绝/风险/限流、重复请求、取消排空：`tests/test_sms_receipt.py`。
- 真实 Chromium 合成 DOM：两游戏普通面板→SVG→二维码；二维码/已扫码未确认/密码→手机号；协议未选不发请求；本人合成点击一次后严格回执；CAPTCHA 暂停探测并在面板消失后接受 Binding；过期二维码不算 ready；扫码未确认不算成功；取消释放。命令 `HOYO_NATIVE_LOGIN_SMOKE=1 python -m pytest -q tests/test_login_dom.py`。
- 受影响双采集/候选/存储拒绝/归属、跨平台 mock 与本机 platform_smoke；Ruff、空模板/独立验证器、打包资源及索引隐私检查。

实际执行数量与结果在 PR 开发结果评论记录；没有执行的检查不得用上表范围替代实测结果。

## 未执行边界与最短本人验收

此修复后真实发码/扫码、身份绑定后的业务采集/保存及跨重启会话复用：NOT_RUN。不能用合成 Cookie、DOM、历史单次 Binding 成功证明真实完整链路通过；短信送达还取决于官方服务与终端。Windows 本人端到端及断电边界：NOT_RUN。外部桌面控制可用性尚未验证。

本人恢复验收时，先更新至固定 PR SHA 后仅运行一次正常同步命令（需要新登录时使用明确方式和允许，在同一窗口完成本人步骤）；由同一 Context 继续双采集和保存，检查最终 PASS、SAVED 或 UNCHANGED、提交/清理/关闭状态。随后 inspect，再关闭并重跑正常命令检查会话复用。全过程不需要临时登录脚本、Profile 复制、Cookie 导出、反复发码或先开一个辅助窗口。失败时保留旧 current，报告固定错误码；不会自行切到另一身份/登录方式。实际路径只在本人私有记录中保留，不贴公开 PR。
