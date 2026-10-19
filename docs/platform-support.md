# 平台边界

登录统一使用当前 AI 客户端内置浏览器，与操作系统无关；Codex 使用 Codex 内置浏览器。规则不代表每个客户端、OS 都已实测。宿主或完整数据读取能力缺失时明确阻塞，见[客户端桥接](client-bridge.md)。

## Python 与宿主

Python 3.12；宿主模块使用 Node.js 内置模块和客户端提供的 browser 对象，不引入独立浏览器驱动。源码和 wheel 均分发 client_host.mjs。解释器由调用方显式提供，不根据 OS 选择浏览器。Windows 与 macOS 的当前版本验收统一见[当前平台状态](release-validation.md#当前平台状态)，不以历史实测替代最新版验收。Linux、其他客户端及真实客户端重启持久化未验收。

Windows IOCP 管道适配只接受已审阅的 CPython 3.12 私有接口结构。启动前对实际使用的构造、关闭、自唤醒和管道接收方法做规范化 AST 指纹检查；未知结构在分配管道前返回 `WINDOWS_ASYNCIO_UNSUPPORTED_RUNTIME`。参考指纹来自 CPython 3.12.14；同结构的补丁版本可用，结构变化须先重新审阅并运行原生契约测试，不静默回退到曾挂起的回环 TCP，也不影响非 Windows 的 asyncio.run 路径。

## 文件系统

结果优先在可核验源码仓库 data/；独立安装回退 platformdirs 系统用户数据根 results/，不依赖 cwd。runtime 根只存 client-runtime-v1 下的会话锁和请求节流元数据，必须在仓库外，不保存认证。

POSIX 对新应用目录使用 0700、文件 0600，核验当前用户所有权和无符号链接；不修改已有共享父目录权限。Windows 使用 SID、owner/DACL、安全句柄及当前用户保护，不以 chmod 代替。拒绝 reparse/junction，Windows 只支持本地固定 NTFS，不支持 UNC、网络盘、云同步或 FAT/exFAT。

## 保存与恢复

写入先刷新文件。POSIX 替换后刷新父目录；Windows 使用文件刷新及 MoveFileEx 的 REPLACE_EXISTING/WRITE_THROUGH。普通用户目录没有等价 POSIX fsync 的承诺，不宣称断电等价。

提交前错误保留旧 current；提交后持久化未知保留新旧代际和日志；已提交的清理失败报告独立待办。恢复核验所有权、身份和完整清单，不能按 UUID 删除。本人旧目录不搜索、不读取、不迁移、不清理。

## 检查与验收

tools/platform_smoke.py 在新临时目录验证权限、跨进程锁、保存/恢复和离线 CLI；不打开官方页面。Windows 的 mock 只证明代码分支；本机 SID/DACL、跨进程锁及当前 Codex 页面采集另有原生验收。Linux 实机与断电行为仍须单独验收。

完整状态见[当前验证](release-validation.md)。
