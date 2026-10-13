# 平台适配与验收边界

本分支基线为 main `0c2a3a7a39474ef80ecc07cec911e9d8c04f1832`。一套游戏采集、验证与代际恢复状态机，平台分支只在薄 I/O 层。**Windows 尚待本人实机端到端验收；不要宣布跨平台正式可用。** 星铁仍暂停；原神仍不含圣遗物与完整面板。

## 目录与权限

结果优先在已核验的源码 clone 的 `data/`（包括普通 pip install .），PEP 610 本地 file URL 通过标准 `urllib.request.url2pathname` 转换；不依赖 cwd。独立安装回退 platformdirs 系统用户数据根的 `results/`。认证根独立于结果覆盖，默认是系统用户数据根；各平台路径见 README。所有默认路径在调用时选择，不在导入时冻结测试或用户环境。

POSIX 仍检查当前 UID、0700/0600、无符号链接；安全打开检查常规文件、单硬链接及路径/句柄身份。Windows 使用当前进程 TokenUser SID、owner/DACL；新对象创建时附带受保护、只允许当前 SID 全权限的 DACL，目录的权限向子对象继承。已有不安全目录拒绝而不修改。公共 clone data/ 只允许当前用户、SYSTEM、Administrators 写入，其下私人身份目录必须只有当前 SID 访问。

Windows 安全打开采用 CreateFile `FILE_FLAG_OPEN_REPARSE_POINT`，随后按句柄核验属性、链接数和 SID/DACL；不共享 DELETE，避免锁文件在持锁期间被替换。路径按未 resolve 的逐层 lstat 拒绝所有 reparse（含 junction、symlink 和云占位符）。私有文件打开后才交 portalocker 做 EXCLUSIVE|NON_BLOCKING 锁；所有锁获取/释放错误均上报，不能当成获得锁。

Windows 当前目标范围为 Windows 11 x64 普通用户、Python 3.12、Playwright Chromium、**本地固定 NTFS 盘**；不支持 UNC、网络/映射盘、FAT/exFAT、可移动盘、云同步目录、目录重定向及设备路径。克隆代码和私人根都应放在本人普通本地目录。系统管理员和持有同一 SID 的恶意进程不属于此应用的隔离边界；路径检查并不承诺抵抗同一身份持续改换父目录的攻击。不提权，不修改系统策略。清点了当前原神/ZZZ 调用链：getuid/O_NOFOLLOW/chmod/fsync 均隔离在平台后端；旧且无调用者的 Git 进程组命令 helper 已删除。唯一 ps 进程诊断在暂停的 HSR 流程，未作为 Windows 可用功能验收。

## 保存语义

写入临时文件后刷新用户态缓冲和文件；代际文件以可写句柄刷新，避免 Windows 对只读句柄的 FlushFileBuffers 限制。POSIX 替换后 fsync 父目录；Windows 调用 MoveFileEx(REPLACE_EXISTING|WRITE_THROUGH)，检查每个调用失败，不降级为普通覆盖。

Windows 没有在本项目普通用户范围可用的 POSIX 目录 fsync 等价。`sync_directory` 的 Windows 分支明确只核验目录，目录变更依赖上述系统替换操作；**不宣称等价断电持久化**。微软对 WRITE_THROUGH 的说明尤其给出 copy/delete 移动的刷新保证，不能把它外推为同卷 rename、目录清理和磁盘硬件断电的证明。这里目标是本地普通进程失败/中断的保存恢复；未验证的强制断电、存储硬件/杀毒软件干预、网络文件系统均不在保证范围。

提交前错误保留旧 current；替换后的持久化检查失败保持 DURABILITY_UNKNOWN 和可能被引用代际；提交后清理失败保持 COMMITTED+CLEANUP_PENDING。恢复仍核验身份、所有权、引用、完整文件清单/哈希及业务验证，不按 UUID 盲删。随 contracts 分发的独立公共验证器仅在 POSIX 对其合成临时文件执行 fchmod；Windows 这些文件继承已保护的代际目录 ACL，代际发布前仍逐项核验/设置私有权限。它不处理认证材料，不能把这一处条件分支用作跳过认证权限检查的模板。

未知或历史格式保留并拒绝，不实现迁移或兼容；本次开发不读取、迁移或清理真实用户目录。

## 依赖语义依据

- [portalocker 3.2.0](https://pypi.org/project/portalocker/3.2.0/) 与 [对应实现](https://github.com/wolph/portalocker/blob/v3.2.0/portalocker/portalocker.py)：对已打开文件句柄加锁；POSIX 默认为 advisory，不能阻止绕过本工具的其他写入者。
- [platformdirs 4.5.0](https://pypi.org/project/platformdirs/4.5.0/) 和 [user_data_path API](https://platformdirs.readthedocs.io/en/latest/api.html)：关闭 appauthor/roaming，选择各平台本地用户数据根；不自动扫描旧根。
- [pywin32 311](https://pypi.org/project/pywin32/311/)：[CreateFile 官方语义](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[MoveFileEx 官方语义](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)。常量归属及绑定签名核对了正式 wheel 和 b311 官方源码，不能仅凭 mock 常量认为真实 API 可用。

## 本人 Windows 验收

在新代码目录检出此 draft PR 的准确 head，确认 `git rev-parse HEAD`。使用本地 NTFS 目录（不要 OneDrive/共享盘）。PowerShell 无需激活脚本：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m hoyo_roster --help
.\.venv\Scripts\python.exe tools\platform_smoke.py
```

smoke 只创建新临时根，运行真实 SID/DACL、身份 key、安全打开、跨进程锁、刷新错误保留 current、提交后清理失败与恢复及 CLI 检查，结束清理自己新建的临时数据。**先确认 PLATFORM_SMOKE=PASS**；失败请报告版本、平台、固定错误代码，不附本机路径或认证材料，不通过提权/放宽权限修复。

然后由本人分别对原神与绝区零执行：

```powershell
.\.venv\Scripts\python.exe -m hoyo_roster --game genshin --uid YOUR_UID --operation init
.\.venv\Scripts\python.exe -m hoyo_roster --game genshin --uid YOUR_UID
.\.venv\Scripts\python.exe -m hoyo_roster --game genshin --uid YOUR_UID --operation inspect
.\.venv\Scripts\python.exe -m hoyo_roster --game zzz --uid YOUR_UID
.\.venv\Scripts\python.exe -m hoyo_roster --game zzz --uid YOUR_UID --operation inspect
```

替换为本人各游戏 UID。需要新登录时应先看到权限说明与二维码/手机号/取消选择；本人完成扫码或手机号、协议确认和图片 CAPTCHA。确认主要阶段提示、准确身份核验、两次采集、SAVED/UNCHANGED、实际 CURRENT_RESULT 内容和字段限制。关闭程序后再次运行，核验会话复用；无变化应保留业务时间戳。不要上传结果、Profile、验证码、截图或完整路径。离线 smoke 通过后仍需这些真实端到端步骤，才讨论正式支持声明。此 PR 保持 draft，本轮不合并。

## 本轮开发证据

实际运行平台为 macOS arm64/Python 3.12；使用临时合成身份和认证根。最终命令、统计及准确交付 SHA 在 PR 交付记录中列出。

早期首轮发生默认路径在导入时固定导致临时认证隔离失效的测试失败；沙箱拒绝该目录操作，未改动已有私人数据。已修为动态路径提供者并在 fixture 显式替换，失败结果不计为通过。随后删除旧根残留允许分支，更新合成保存拒绝断言。Windows 模拟只证明分支策略，不证明 SID/DACL 创建、原生句柄、Win32 锁、真实替换、Chromium 或用户端流程；这些在 macOS 上均 NOT_RUN，断电行为也 NOT_RUN。

最终开发检查（各组有重叠，不相加为唯一用例数）：

| 实际命令（源码命令前置 `PYTHONPATH=src`，python 为项目虚拟环境） | 结果 |
|---|---|
| `python -m pytest -q tests/test_platform_io.py tests/test_session_handoff.py tests/test_login_flow.py tests/test_login_ux.py tests/test_game_chains.py tests/test_manual_captcha.py tests/test_login_contract.py -x` | PASS：206 passed；之后新增的一项 Windows 正向句柄 mock 在下方最终 35 项中验证 |
| `python -m pytest -q tests/test_storage_faults.py tests/test_output_paths.py tests/test_direct_entrypoint.py tests/test_candidate_ownership.py tests/test_sync_storage_rejection.py tests/test_recovery_entrypoint.py -x` | PASS：109 passed |
| `python -m pytest -q tests/test_direct_entrypoint.py tests/test_output_paths.py tests/test_platform_io.py -x` | PASS：78 passed |
| `python -m pytest -q tests/test_recovery_atomicity.py -k 'valid_and_repeated_recovery or explicit_bad_inventory or damaged_partial or permission or bound or unowned' -x` | PASS：39 passed，10 skipped，110 deselected；skip 为无写入/无清理阶段的参数组合 |
| `python -m pytest -q tests/test_platform_io.py tests/test_sync_storage_rejection.py tests/test_game_chains.py -x` | PASS：35 passed；包含最终安全打开、Win32 策略 mock 与移除未调用 helper 后的业务链检查 |
| `python tools/platform_smoke.py` | PASS：真实 macOS 私有权限、跨进程锁、保存/恢复、身份 key、CLI；源码及干净 wheel 安装均通过 |
| 原神/ZZZ `persistent_browser`，全新临时 Profile，离线 `about:blank` | PASS：真实 Chromium 启动、Profile 权限与 Context/运行时关闭；Chromium 仅安装在临时目录。首次受沙箱启动限制，获运行环境批准后通过 |
| `python -m ruff check src tests tools --select F`；`python -m ruff format --check src tests tools` | PASS |
| `python tools/validate_repository.py --require-empty-baseline`；`--self-test` | PASS：空公开基线；14 个正向 fixture、81 个负向规则 |
| `python -m build`；`python tools/verify_distribution.py`；干净临时 venv 安装 wheel、三游戏模块导入及 installed contracts 验证 | PASS：wheel/sdist，91 个包资源，许可证随包保留；CLI help 使用统一平台路径说明 |
| `python tools/check_private_data.py`；`git diff --check` | PASS：只检查 Git 索引路径及公开 diff，不读取私人内容 |
| Windows SID/DACL/句柄/锁/替换/Chromium/真实账号端到端；Linux/WSL 平台；断电持久化 | NOT_RUN：无对应实机环境或未授权真实登录；mock 不能替代 |

另一次较早开发回归为 363 passed / 10 skipped；它发生在后续安全读取和常量/句柄细化前，只作开发记录，不冒充最终提交逐项重跑。Git 索引 diff 检查最初将原样保留的 Windows 上游许可证 CRLF/行尾空白报告为格式错误；已在仓库 .gitattributes 仅为这些许可证声明保留上游空白，未改全局设置或改写许可证。早期失败均已修正，最终上述命令无未解决失败。没有 CI/Actions、独立审计、合并、仓库重建或隐私历史清理；提交仅含代码、合成测试和公开文档/许可证。
