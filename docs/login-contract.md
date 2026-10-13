# 官方公开登录契约证据

以下是 2026-10-12 独立审查保存的官方静态前端源码证据。本轮复核了保存文件字节哈希及模块字符串，未调用认证 API、创建 QR ticket、发送短信或登录账号。仅用于 page-scoped HSR 登录阶段 allowlist 预检，不能代替真实账号/页面验收。

| 官方来源 | SHA-256 | 确认内容 |
|---|---|---|
| [verifier 模块](https://user.mihoyo.com/login-platform/js/5823.9f588959.js) | `c516fce638a759b0ef44c1eaa326c74794204ac98ade239b8562c272a3d6430d` | `/account/ma-cn-verifier/verifier` 前缀加 `createLoginCaptcha`，由公共 POST helper 提交 |
| [登录模块](https://user.mihoyo.com/login-platform/js/login.35d0ab6d.js) | `afe0e060f2d1b7f456f4f2d2453ad33648923920811c587e8e17db4b27453d97` | `/account/ma-cn-passport/web` 前缀加 `loginByMobileCaptcha`、`createQRLogin`、`queryQRLoginStatus`，由同一 POST helper 提交 |
| [官方 SDK](https://webstatic.mihoyo.com/dora/biz/mihoyo-account-sdk/main.js) | `768c2a73b0fea20e1b17daf96f72806a81ae867b61efeaf0ffb400ba2c7bd604` | 官方账号 SDK 使用 passport-api.mihoyo.com 和分阶段会话初始化；SDK 本身不是所有延迟登录模块的全集 |

准许的新增四条 POST 完整路径分别是：

- `https://passport-api.mihoyo.com/account/ma-cn-verifier/verifier/createLoginCaptcha`
- `https://passport-api.mihoyo.com/account/ma-cn-passport/web/loginByMobileCaptcha`
- `https://passport-api.mihoyo.com/account/ma-cn-passport/web/createQRLogin`
- `https://passport-api.mihoyo.com/account/ma-cn-passport/web/queryQRLoginStatus`

旧 `/account/loginByMobileCaptcha` 已不在当前登录允许列表。保留精确官方会话 verifyCookieToken 和设备初始化 getFp 契约；只有官方页面自然发出这些请求。业务写入、其他 POST、伪造 host、路径后缀、非 HTTPS 和无关方法继续拒绝；登录阶段禁止 rpgcultivate 业务请求，完成登录后封闭页面并切换业务 guard。代码不直接代调这四条认证 API，也不自动填写手机号/验证码或扫码。

三个游戏的官方网页是否在当前账号环境提供二维码/手机号入口仍是 **NOT_RUN**。命令选择方式后只读取入口可见性，未观察到就明确停止；没有把静态源码推断说成实际网页能力。
