# 本机使用

## 首次使用

按 README 安装 Python 3.12、项目和 Playwright Chromium。在源码目录之外保存个人结果；可用 `--storage-root /absolute/private/path` 选择新目录。程序逐层把**本轮新建目录**设为 0700，不修改已存在的共享父目录。已存在的 storage-root 必须属于当前用户且为 0700，否则报 `UNSAFE_PRIVATE_DIRECTORY`；请选择新的私有子目录。普通结果和程序元数据为 0600，浏览器内部文件权限由 Chromium 管理并被 0700 Profile 包围。

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation init
hoyo-roster --game genshin --uid YOUR_UID --operation login
hoyo-roster --game genshin --uid YOUR_UID
```

`init` 只建立验证后的空工作区，不联网。`login` 检查精确官方 Binding，不采集角色。默认 `sync` 检查会话、核验身份、采集第1次和第2次、验证、保存或报告无变化；无变化保留业务时间戳。相同游戏/UID 始终选择同一隔离范围；不同游戏/UID 使用独立 Profile、锁和节流状态。

交互终端在会话失效时选择 1 二维码、2 手机号、3 取消。本人在前台官方页面打开登录面板并选择对应入口，扫码/手机号/验证码/确认均由本人完成，终端不接收认证信息。程序只检查入口是否可见，30 秒内未观察到入口报 `LOGIN_METHOD_UNAVAILABLE`；不自动改选另一方式。具体游戏的实际二维码/手机号页面尚待真实浏览器验收。

非交互进程使用 `--non-interactive --login-method qr|phone|cancel`；缺失显式选择且需要登录时停止。`--login-timeout 900` 是默认 Binding 等待上限，范围 0 < 秒数 ≤ 3600；所选入口检测最多 30 秒。`--headless` 复用已验证会话，需交互时停止。Ctrl-C 取消会释放锁并关闭本次浏览器，正在进行的本机写入会先结束再释放锁。

## 升级

关闭本工具，更新或重新安装代码，然后运行原命令。程序从发行包携带的 contracts 生成新代际，复制本身份已保存数据，做完整验证，再替换 current 指针。升级不复制其他身份的 Profile，不从旧私有仓库导入数据。当前版本不提供旧 Broker、Runtime、users.toml 或候选格式迁移。

## 检查与恢复

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation inspect
hoyo-roster --game genshin --uid YOUR_UID --operation recover
```

inspect 在 current 缺失或失效时仍给出 `EMPTY` / `INVALID`、代际 UUID 与 `VERIFIED` / `UNVERIFIED`、是否有恢复日志和候选状态；不输出 UID、路径或 JSON 内容。UUID 是本机恢复标识，也应在公开反馈时脱敏。

保存有三个边界：

- `NOT_COMMITTED`：新 current 未提交。之前的数据仍有效，已写恢复日志时必须 recover 后再运行。
- `COMMITTED`：新 current 已替换且父目录 fsync 成功。旧代际或候选清理失败仅是清理待办，已经保存的数据有效；不会把保存说成失败。
- `DURABILITY_UNKNOWN` / `COMMIT_DURABILITY_UNKNOWN`：替换已发生，父目录持久化失败。新旧代际和恢复日志都保留，不声称完成保存，停止采集并检查恢复。

recover 在锁内核验身份范围、程序所有权、当前引用、文件权限、完整数据和验证器，然后确认指针持久化，清理日志指明的非当前代际。若清理仍失败，继续报告待办，不删除 current。日志或代际来源不明时停止，不能因为目录名是 UUID 就删除。

current 失效时，先 inspect，选取本人确认且显示 VERIFIED 的代际：

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation recover --generation VERIFIED_GENERATION_UUID
```

显式恢复会重新完整验证目标并切换指针；`UNVERIFIED`、别的身份或任意 UUID 都被拒绝。未知/损坏残留保留给人工核验，不提供强制忽略验证的删除开关。

清理中断而只剩部分旧代际时，recover 依据提交前已完整验证并持久化的文件清单，核对现存路径/哈希/所有权；出现新文件或改动就拒绝。恢复日志缺失时，先保留 current，用 inspect 选择完整 VERIFIED 的非当前代际，执行 `--operation cleanup-generation --generation VERIFIED_GENERATION_UUID`；当前代际和无法证明归属的残留不能删。

候选残留与代际恢复分别处理。只有程序能证明归属的候选才允许清理：

```bash
hoyo-roster --game genshin --uid YOUR_UID --operation cleanup-candidate --run-id OWNED_CANDIDATE_UUID
```

## 常见停止和建议

| 状态 | 下一步 |
|---|---|
| `AUTHENTICATION_REQUIRED` / `LOGIN_TIMEOUT` | 本人检查官方登录状态，明确选择方式后重新运行 |
| `LOGIN_CANCELLED` / `INTERRUPTED` | 本人准备好后重新运行 |
| `LOGIN_METHOD_UNAVAILABLE` | 检查官方是否提供该入口，取消或显式选择另一方式 |
| `WRONG_ACCOUNT` / Binding 错误 | 确认本人游戏、UID、区服；不要借其他会话绕过 |
| `RATE_LIMITED` / `RISK_CONTROL` | 停止本轮，按官方提示处理；不自动重试或代解验证 |
| 双次漂移 / schema / lifecycle 错误 | 旧 current 保留，等待来源稳定或提交脱敏问题反馈 |
| `USER_WORKSPACE_BUSY` | 等另一同身份进程退出，不删锁文件来绕过 |
| Profile 在用或 Singleton 残留 | 关闭确切本工具浏览器并检查；不删除正在使用的 Profile |
| `RECOVERY_REQUIRED` / 持久化未知 / residue | inspect 后按上节恢复；不随意删 UUID 目录 |
| Chromium 缺失 | 在相同虚拟环境运行 `python -m playwright install chromium` |

## 退出登录与卸载

浏览器关闭后执行 `--operation logout`：只删除该身份 Profile 和已验证身份指纹；角色数据保留。发现 Singleton 标识时停止，要求先检查是否仍有浏览器进程。退出登录不会退出其他应用的米游社会话。

卸载代码：`python -m pip uninstall hoyo-roster`；这不删除任何个人结果或 Playwright 浏览器。需要彻底清理时，关闭所有本工具进程，按 privacy 文档核对范围后自行删除选定身份范围；保留数据或先做私有备份。不要把整个共享父目录作为清理目标。
