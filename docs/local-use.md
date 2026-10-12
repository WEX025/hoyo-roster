# 每个人维护自己的账号

新入口只采集并保存到本机，不联网发布，不执行 Git fetch/commit/push。代码可来自干净源码目录、自己的仓库或私有原仓库；不要求朋友有原作者仓库的权限。无需在线多租户服务。

## 安装与首次使用

在拿到经检查的源码目录后，以 Python 3.12 安装现有四个包：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ./skills/hoyo-browser-session -e ./skills/genshin-character-state-sync -e ./skills/zzz-character-state-sync -e ./skills/hsr-character-state-sync
.venv/bin/python -m playwright install chromium
```

正常使用无需配置文件。把 `YOUR_UID` 替换成你自己的该游戏 UID：

```bash
.venv/bin/python tools/run_local_hoyo.py --game genshin --uid YOUR_UID
.venv/bin/python tools/run_local_hoyo.py --game zzz --uid YOUR_UID
.venv/bin/python tools/run_local_hoyo.py --game hsr --uid YOUR_UID
```

默认 `sync`，有界面；自动选择由游戏与 UID 决定的私有目录，不会读取或写入 `users.toml`。
第一次使用或会话过期时，由本人在打开的官方页面完成登录；命令等待精确 Binding 后继续。
不读 stdin，不自动输入密码/验证码。UID 是身份选择而非访问授权。原神需要 `hk4e_cn`
以及 Binding 确认的 `cn_gf01`/`cn_qd01`；绝区零和星铁使用其国服游戏 Binding 和 `prod_gf_cn`。
隐私、登录、绑定、验证码和风控限制仍然有效，不借用其他浏览器身份、不绕过权限、不自动重试。

只想检查登录或初始化目录时，分别加 `--operation login`、`--operation init`。
`init` 不启动浏览器。已验证登录可以加 `--headless`，但首次或失效登录必须有界面；
headless 下身份不匹配或缺少登录会立即失败。登录仍有有效期，Cookie 持久化不保证永久登录。

### 高级配置方式（兼容旧入口）

```bash
mkdir -p ~/.config/hoyo-game-data
cp examples/users.example.toml ~/.config/hoyo-game-data/users.toml
chmod 600 ~/.config/hoyo-game-data/users.toml
```

只在首次建立高级配置时复制示例；以后不要覆盖自己的配置。填自己的 UID，删除不用的游戏。
配置为当前用户所有的 `0600` 文件，`storage_root` 是仓库外绝对路径（可用 `~`）。
高级模式仍使用 `.venv/bin/python tools/run_local_hoyo.py --user me sync genshin`；可加
`--config /你的外部配置路径/users.toml`。不接受凭据配置键，不允许和 `--game/--uid/--operation`
直接模式混用。冲突返回 `ARGUMENT_MODE_CONFLICT`，不会猜测哪个身份有效。

原神输出仍为 `partial`；绝区零输出仍为 `complete`；星铁为 `partial`，来源无法证明完整角色池时允许仅个人档案。每次同步仍运行两次业务采集、一致性与生命周期检查、候选验证、完整安装验证。失败不替换旧角色池；无变化保留原 `synced_at`。

## 输出与隔离

直接模式默认存储目录为 `~/.local/share/hoyo-roster/`；可用 `--storage-root` 指定另一个外部绝对目录。高级模式保留配置中的存储根。目录布局：

```text
users/<作用域别名>/workspace.lock     # 同一作用域串行运行
users/<作用域别名>/<game>/<身份摘要>/
  browser-profile/                   # 该游戏/UID 的私有登录状态
  current.json                       # 当前 generation 名称；不含 UID
  <generation>/data/<完整游戏名称>/  # profile、roster、builds；ZZZ 另有 bangboos
  candidates/                        # 临时候选；正常完成后删除
```

用 `current.json` 的 `generation` 字段定位当前数据。读取指针一次，再读取该目录中的数据；升级或同步会原子切换指针，旧临时代际随后清理，不保留历史角色池。需要稳定读取时应取得同一个 `workspace.lock`，不要在同步期间持续使用旧目录路径。直接模式的作用域别名由游戏和 UID 的摘要生成，不包含明文 UID。不同游戏/UID 的数据和浏览器 Profile 均独立；重复执行使用原目录。高级模式别名仍保留，数据路径不变，但 Profile 也改为每个游戏/UID 独立。旧的同别名共享 Profile 留在原处，不自动复制、删除或复用，首次升级后可能需要本人重新登录。

目录为 `0700`，个人数据文件为 `0600`。消费者不要再默认读取代码 checkout 的 `data/`；本地入口的个人数据权威是当前外部 workspace 中的 `data/`。该 workspace 仅有当前游戏的个人资料，另外两游戏为生成的空基线。浏览器状态不属于正式数据或备份输出，不应上传、打包或提交。

`CONFIG_MISSING`、`USER_NOT_CONFIGURED`、`GAME_NOT_CONFIGURED` 指示缺少配置；`INVALID_CONFIG` 或 `UNSAFE_CONFIG` 指示格式/权限错误。`USER_WORKSPACE_BUSY` 表示同一人另一个命令持锁。异常只输出固定代码，不输出账号、响应或凭据。进程中断如留下代际或候选残留，会失败关闭；保留残留和当前指针，先检查，不要手动猜测删除或启动重试：

```bash
.venv/bin/python tools/run_local_hoyo.py --game genshin --uid YOUR_UID --operation inspect
.venv/bin/python tools/run_local_hoyo.py --game genshin --uid YOUR_UID --operation cleanup-candidate --run-id UUID_FROM_INSPECT
```

清理命令只接受游戏 Candidate Authority 验证的本地用户候选，不清理旧入口的共享目录。代际残留只报告数量，没有自动删除接口；需要人工核实当前指针和残留验证结果后再恢复，不能仅凭目录名称猜测安全性。

## 升级和迁移

更新源码或拉取自己的代码仓库后，用同一安装命令重新安装四包；不要重新复制示例覆盖自己的配置，也不要移动存储目录。新入口会在临时代际中更新公开 Schema/Validator，复制仅当前身份的现有数据，完整验证后切换，保留配置、角色池和登录 Profile。新 Schema 不兼容时保留旧当前代际并报错，不自动重置数据。直接模式也使用稳定游戏/UID 作用域，升级不更改这个路径。

旧 `tools/run_hoyo_sync.py` 和 Broker `publish-main` 是保留的固定仓库/Runtime 兼容代码，
不适用于这里的新用户。使用 `run_local_hoyo.py`；它不会向 GitHub 写入个人资料。

如你本人已有同格式的旧数据并希望迁移，可在直接模式加 `--operation migrate`，或在高级模式运行 `--user me migrate genshin`。命令仅读取当前
checkout 中选定游戏，核对配置 UID，要求外部目标为 `not_synced`，完整验证后安装本地副本。
不会删除源文件，不迁移登录状态，不覆盖已有本地采集结果；身份不匹配拒绝。新使用者从
本仓库的空基线开始，不应导入他人的数据。

原神首次期望身份由显式 UID 在内存建立，随后必须通过官方 Binding 核对 UID、游戏和支持区服。
不会填入未经验证的 Profile。离线回归已覆盖真实 Provider/协调流程与合成双次采集；仍未完成真人登录实测。

直接模式的 `GAME_REQUIRED`、`UID_REQUIRED`、`INVALID_UID` 表示参数缺失或格式错误。
`ARGUMENTS_INVALID` 表示解析错误；`ARGUMENT_MODE_CONFLICT` 表示直接和配置模式混用。
错误输出不回显 UID 或其他参数值。UID 必须是非零开头、最多 32 位的 ASCII 数字字符串。

## 开发存储边界

`hoyo_browser_session.user_workspace.SnapshotStore` 只要求 `candidate_base` 和 `commit(game_dir)`。三游戏 `run_sync(..., snapshot_store=store)` 继续拥有来源、双次一致性、生命周期和候选验证；存储负责锁、身份隔离和完整安装。提供的 `LocalSnapshotStore` 使用当前代际指针，禁止和 Apply/Browser Owner 发布权限组合。替换存储实现须保留同样的隔离、完整验证和原子安装约束。此次没有引入新依赖。

通用角色/武器 ID、Schema 和代码可分享；“我拥有哪些角色、等级、装备、面板、UID、昵称”是私人角色池。当前仓库没有独立的公开角色百科，公共机制/倍率/环境资料仍应按原架构实时获取，不从私人 roster 推导公共角色表。
