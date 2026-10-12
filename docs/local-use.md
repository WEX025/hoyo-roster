# 每个人维护自己的账号

新入口只采集并保存到本机，不联网发布，不执行 Git fetch/commit/push。代码可来自干净源码目录、自己的仓库或私有原仓库；不要求朋友有原作者仓库的权限。无需在线多租户服务。

## 安装与首次使用

在拿到经检查的源码目录后，以 Python 3.12 安装现有四个包：

```bash
python3.12 -m venv .venv
.venv/bin/pip install ./skills/hoyo-browser-session ./skills/genshin-character-state-sync ./skills/zzz-character-state-sync ./skills/hsr-character-state-sync
.venv/bin/python -m playwright install chromium
mkdir -p ~/.config/hoyo-game-data
cp examples/users.example.toml ~/.config/hoyo-game-data/users.toml
chmod 600 ~/.config/hoyo-game-data/users.toml
```

编辑仓库外的 `users.toml`：把合成 UID 换成自己的 UID，删除不用的游戏。保留 `version = 1`；`storage_root` 必须是仓库外的绝对路径，可使用 `~`。配置不接受 Cookie、token、密码、模块名称或命令。配置必须由当前用户所有且为 `0600`。示例不是可登录的账号。

```bash
.venv/bin/python tools/run_local_hoyo.py --user me init genshin
.venv/bin/python tools/run_local_hoyo.py --user me login genshin
.venv/bin/python tools/run_local_hoyo.py --user me sync genshin
.venv/bin/python tools/run_local_hoyo.py --user me sync zzz
.venv/bin/python tools/run_local_hoyo.py --user me sync hsr
```

默认有界面，首次登录由本人在官方页面完成；命令会等待精确账号绑定，不读取 stdin。`login` 只检查登录，`sync` 也可在需要时等待登录。已登录时可以加 `--headless`；失效、账号不匹配或首次登录时会失败关闭。禁止以别人的 Cookie 或其他浏览器身份代替登录。当前源只支持国服：原神 `hk4e_cn`、绝区零 `nap_cn`、星铁 `hkrpg_cn / prod_gf_cn`；不承诺国际服或未核实接口可用。

UID 是选择和核对身份的参数，不是授权。当前三游戏实现均检查官方登录绑定：原神读取官方养成计算器自然请求；绝区零读取官方账号记录和养成接口；星铁先建立官方养成会话再采集。都不是“仅 UID 获取全部私人角色池”的服务。公开展示柜等第三方 UID 接口可能只显示用户公开的部分角色，本项目没有实现这些来源，也不把展示柜当成完整角色池。官方隐私/展示设置、绑定、登录、验证码或风控限制仍然有效；遇到拒绝、限流或风控停止，不自动绕过或重试。

原神输出仍为 `partial`；绝区零输出仍为 `complete`；星铁为 `partial`，来源无法证明完整角色池时允许仅个人档案。每次同步仍运行两次业务采集、一致性与生命周期检查、候选验证、完整安装验证。失败不替换旧角色池；无变化保留原 `synced_at`。

## 输出与隔离

默认存储目录为 `~/.local/share/hoyo-game-data-local/`：

```text
users/<本地别名>/browser-profile/       # 本人的浏览器登录状态
users/<本地别名>/workspace.lock       # 同一人各游戏串行运行
users/<本地别名>/<game>/<身份摘要>/
  current.json                       # 当前 generation 名称；不含 UID
  <generation>/data/<完整游戏名称>/  # profile、roster、builds；ZZZ 另有 bangboos
  candidates/                        # 临时候选；正常完成后删除
```

用 `current.json` 的 `generation` 字段定位当前数据。读取指针一次，再读取该目录中的数据；升级或同步会原子切换指针，旧临时代际随后清理，不保留历史角色池。需要稳定读取时应取得同一个 `workspace.lock`，不要在同步期间持续使用旧目录路径。别名、游戏或 UID 不同都会得到独立数据目录；同别名的游戏共享这个人的登录 Profile。第二个人用另一个别名；换另一个米哈游账号时也应新建别名，避免登录状态相互覆盖。

目录为 `0700`，个人数据文件为 `0600`。消费者不要再默认读取代码 checkout 的 `data/`；本地入口的个人数据权威是当前外部 workspace 中的 `data/`。该 workspace 仅有当前游戏的个人资料，另外两游戏为生成的空基线。浏览器状态不属于正式数据或备份输出，不应上传、打包或提交。

`CONFIG_MISSING`、`USER_NOT_CONFIGURED`、`GAME_NOT_CONFIGURED` 指示缺少配置；`INVALID_CONFIG` 或 `UNSAFE_CONFIG` 指示格式/权限错误。`USER_WORKSPACE_BUSY` 表示同一人另一个命令持锁。异常只输出固定代码，不输出账号、响应或凭据。进程中断如留下代际或候选残留，会失败关闭；保留残留和当前指针，先检查，不要手动猜测删除或启动重试：

```bash
.venv/bin/python tools/run_local_hoyo.py --user me inspect genshin
.venv/bin/python tools/run_local_hoyo.py --user me cleanup-candidate genshin --run-id <检查得到的UUID>
```

清理命令只接受游戏 Candidate Authority 验证的本地用户候选，不清理旧入口的共享目录。代际残留只报告数量，没有自动删除接口；需要人工核实当前指针和残留验证结果后再恢复，不能仅凭目录名称猜测安全性。

## 升级和迁移

更新源码或拉取自己的代码仓库后，用同一安装命令重新安装四包；不要重新复制示例覆盖自己的配置，也不要移动存储目录。新入口会在临时代际中更新公开 Schema/Validator，复制仅当前身份的现有数据，完整验证后切换，保留配置、角色池和登录 Profile。新 Schema 不兼容时保留旧当前代际并报错，不自动重置数据。

旧 `tools/run_hoyo_sync.py` 和 Broker `publish-main` 是保留的固定仓库/Runtime 兼容代码，
不适用于这里的新用户。使用 `run_local_hoyo.py`；它不会向 GitHub 写入个人资料。

如你本人已有同格式的旧数据并希望迁移，可显式运行 `migrate <game>`。命令仅读取当前
checkout 中选定游戏，核对配置 UID，要求外部目标为 `not_synced`，完整验证后安装本地副本。
不会删除源文件，不迁移登录状态，不覆盖已有本地采集结果；身份不匹配拒绝。新使用者从
本仓库的空基线开始，不应导入他人的数据。

原神新空基线首次登录当前有已知 bootstrap 缺口，尚未按配置 UID 建立期望身份；详见 README。
不要通过填入未经绑定验证的账号字段绕过检查。离线自测不等于已验证真实首次登录采集。

## 开发存储边界

`hoyo_browser_session.user_workspace.SnapshotStore` 只要求 `candidate_base` 和 `commit(game_dir)`。三游戏 `run_sync(..., snapshot_store=store)` 继续拥有来源、双次一致性、生命周期和候选验证；存储负责锁、身份隔离和完整安装。提供的 `LocalSnapshotStore` 使用当前代际指针，禁止和 Apply/Browser Owner 发布权限组合。替换存储实现须保留同样的隔离、完整验证和原子安装约束。此次没有引入新依赖。

通用角色/武器 ID、Schema 和代码可分享；“我拥有哪些角色、等级、装备、面板、UID、昵称”是私人角色池。当前仓库没有独立的公开角色百科，公共机制/倍率/环境资料仍应按原架构实时获取，不从私人 roster 推导公共角色表。
