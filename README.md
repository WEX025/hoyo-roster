# hoyo-roster

在本机把你本人国服原神、绝区零或崩坏：星穹铁道的官方账号状态保存成私有 JSON。显式选择游戏和 UID，复用该身份独立的浏览器会话；需要登录时由本人在官方页面扫码或输入手机号和验证码。采集两次并验证一致后才保存。

当前版本 **0.1.0rc1 是发布候选**。真实验收 worker 已在前置源码 `52bb6e177b30020b5d313cf2e23996fa2840944d` 确认原神、绝区零的本人身份、关闭后新进程认证复用、双次采集、保存、完整数据仓库验证和 inspect 通过。图片 CAPTCHA 交接改动源码 `345e097f5d028fc3c21ec1e1485c977fbc2059b1` 的合成定向及受影响回归为 **205 passed**；该 SHA 未重新执行真实登录或 CAPTCHA 界面验收。星铁真实全流程、所有扫码/短信入口和其他平台不在已验收范围。版本号相同不代表源码相同；固定 SHA 与边界见[最新交付记录](docs/release-validation.md)。

## 最短开始

本轮已验证的开发/安装平台为 macOS arm64、Python 3.12。代码使用 `fcntl`，不支持原生 Windows；Linux/WSL 尚未完成本轮平台验收。只支持代码中明确允许的中国大陆区服：原神 `cn_gf01` / `cn_qd01`，绝区零和星铁 `prod_gf_cn`。国际服、其他区服、其他登录提供方不在当前契约内。

```bash
git clone --branch release/first-candidate https://github.com/WEX025/hoyo-roster.git
cd hoyo-roster
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m playwright install chromium
hoyo-roster --game genshin --uid YOUR_UID
```

把 `YOUR_UID` 换成自己的游戏 UID；它只用于选择身份，官方 Binding 必须同时证实游戏、UID 和区服。`--game` 可为 `genshin`、`zzz`、`hsr`。`python -m hoyo_roster` 与 `hoyo-roster` 使用同一个入口，无需 users.toml、私有数据仓库、后台服务或 GitHub 登录。Chromium 由 Playwright 另行安装，本项目发行包不捆绑浏览器。

需登录时终端询问 **二维码 / 手机号 / 取消**。自动化调用请显式提供 `--non-interactive --allow-login --login-method qr`（或 `phone` / `cancel`）；`--allow-login` 表示已阅读登录前说明并允许本机手动登录与会话复用，仍需本人在官方页面阅读协议、扫码或输入手机号/验证码并确认；未知新协议和额外权限须另行确认。会话有效时直接复用，`--headless` 只适用于已登录的精确身份。程序等待所选入口加载，超时报 `LOGIN_TIMEOUT`，操控不可用报 `LOGIN_CONTROL_UNAVAILABLE`；没有观察到入口不等于官方不支持该方式。

```bash
hoyo-roster --game zzz --uid YOUR_UID --login-method phone
hoyo-roster --game hsr --uid YOUR_UID --operation login --login-method qr
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
```

遇图片 CAPTCHA，终端显示 `WAITING_FOR_MANUAL_CAPTCHA` 并说明对应的官方浏览器登录窗口：**请本人自己点击图片或操作滑块，AI 不自动选择、不解图或绕过**。保持该窗口和命令运行，程序保留同一会话并暂停自动身份探测；面板消失后继续本人发码、登录和精确身份核验。程序不自动发码或重发，页面倒计时不代表短信实际送达；面板消失也不代表登录成功。Ctrl-C 可取消，超时或窗口关闭则停止；Profile 保留供重新运行复用。其他风控和限流仍停止。

默认数据在 `~/.local/share/hoyo-roster` 的游戏/UID 哈希隔离目录中。正常同步仅保存本机数据，不修改源码、不提交或上传个人结果。UID 出现在你输入的命令中，可能留在 shell 历史和进程参数里；程序输出不打印 UID 或凭据。

阅读 [首次使用、升级与恢复](docs/local-use.md)、[数据完整度](docs/data-format.md)、[隐私和清理](docs/privacy.md)、[开发指南](CONTRIBUTING.md)、[验证记录](docs/release-validation.md)。

## 数据限制

原神输出 `partial`，拒绝无法证明闭合的列表和未知多页契约。绝区零仅在名单、养成名单、逐角色详情、批量详情和邦布闭合条件全部满足时输出 `complete`。星铁输出 `partial`：可能只刷新 Profile；来源未证明 owned roster 时保留此前角色数据，它们可能仍是旧值。`synced_at` 不意味着每个旧字段都重新采集。

## 许可证

本项目代码和项目文档采用 [MIT](LICENSE)，版权 2026 WEX025。依赖通知见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。MIT 不授予米哈游素材、商标或接口使用权。本项目不是米哈游官方产品，不包含游戏素材；官方服务行为和可见入口仍以官方页面为准。
