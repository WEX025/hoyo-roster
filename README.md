# hoyo-roster

在本机把你本人国服原神、绝区零或崩坏：星穹铁道的官方账号状态保存成私有 JSON。显式选择游戏和 UID，复用该身份独立的浏览器会话；需要登录时由本人在官方页面扫码或输入手机号和验证码。采集两次并验证一致后才保存。

当前版本 **0.1.0rc1 是发布候选**。自动化合成回归与真实账号验收分别记录；真实登录、短信、扫码和三游戏真实全流程尚未完成，不能据此认定发布验收通过。

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

需登录时终端询问 **二维码 / 手机号 / 取消**。自动化调用请显式提供 `--non-interactive --login-method qr`（或 `phone` / `cancel`）；这只是选择方式，仍需本人操作官方页面。会话有效时直接复用，`--headless` 只适用于已登录的精确身份。官方页面若未显示所选入口，程序明确停止，不声称该方式已可用。

```bash
hoyo-roster --game zzz --uid YOUR_UID --login-method phone
hoyo-roster --game hsr --uid YOUR_UID --operation login --login-method qr
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
```

默认数据在 `~/.local/share/hoyo-roster` 的游戏/UID 哈希隔离目录中。正常同步仅保存本机数据，不修改源码、不提交或上传个人结果。UID 出现在你输入的命令中，可能留在 shell 历史和进程参数里；程序输出不打印 UID 或凭据。

阅读 [首次使用、升级与恢复](docs/local-use.md)、[数据完整度](docs/data-format.md)、[隐私和清理](docs/privacy.md)、[开发指南](CONTRIBUTING.md)、[验证记录](docs/release-validation.md)。

## 数据限制

原神输出 `partial`，拒绝无法证明闭合的列表和未知多页契约。绝区零仅在名单、养成名单、逐角色详情、批量详情和邦布闭合条件全部满足时输出 `complete`。星铁输出 `partial`：可能只刷新 Profile；来源未证明 owned roster 时保留此前角色数据，它们可能仍是旧值。`synced_at` 不意味着每个旧字段都重新采集。

## 许可证

本项目代码和项目文档采用 [MIT](LICENSE)，版权 2026 WEX025。依赖通知见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。MIT 不授予米哈游素材、商标或接口使用权。本项目不是米哈游官方产品，不包含游戏素材；官方服务行为和可见入口仍以官方页面为准。
