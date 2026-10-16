# 可重复计时采集

普通同步仍默认串行。需要原神和绝区零完整计时或串并行比较时，使用仓库的 `tools/timed_collection.mjs`，不再复制实验目录中的脚本。游戏采集实现、双轮、身份和保存契约沿用 [AGENTS](../AGENTS.md)。此入口随源码及 sdist 分发；单独 wheel 内含底层 `client_runner.mjs`，不含仓库 tools/Skill。需要完整编排时保留源码 checkout。

## 更新与预检

仅在用户要求更新时检查 `git status --short`、`git fetch origin` 和当前分支与远端的差异。干净且可快进时 `git pull --ff-only`；有未提交修改或分叉时保留现场，判断分支用途。采集入口不会隐式拉取或切换代码，也不每次无条件重装。

选择实际 Python 3.12 环境，先运行 `python tools/verify_installation.py`。它按字节核对全部 Python/JavaScript/资源清单，拒绝多余安装文件和脏源码；仅安装不匹配时构建并重装，重试预检。源码改动应先完成验证并提交，再以干净版本采集。开发测试可用 `PYTHONPATH=src`，实际安装核验不要设置它来掩盖旧安装。

## 前台执行

按当前客户端浏览器工具的文档取得真实内置浏览器对象。将下列配置填成实际绝对路径；不要导出认证材料。先用客户端支持的前台执行超时 270 秒调用：

```js
var { startTimedCollection } = await import(REPOSITORY_FILE_URL + "/tools/timed_collection.mjs");
var collection = await startTimedCollection(browser, {
  python: PYTHON_312_EXECUTABLE,
  directory: NEW_PRIVATE_RUN_DIRECTORY,
  runtimeRoot: SHARED_RUNTIME_DIRECTORY,
  parallelGames: true,
});
console.log(await collection.advance());
```

配置变量由执行者选择；`REPOSITORY_FILE_URL` 是 checkout 的 file URL。默认 `parallelGames:false`；比较时只改变此参数及新的 run 目录，其他条件保持一致。入口再次实时运行版本预检，安全创建新的私有目录；目录已存在或位于 checkout 内会拒绝，避免覆盖旧证据及污染源码。

下一次主动调用只运行 `await collection.advance()`，直至 `done:true`。每段最多 180 秒启动预算，内部窗口最多十秒，收拢最长 45 秒来源后返回；实际墙钟可超过启动预算。不要开后台循环，不要在请求仍待推进时只等待 worker completion。工具外层异常时，在可执行的前台调用中 `await collection.cancel()`。

登录 question 立即返回；遵守 [Skill](../.agents/skills/local-roster/SKILL.md) 的 attempt 规则，由用户在页面完成登录并明确确认后，调用 `collection.host.answerLogin("completed", question.attempt)`，再继续同一对象。入口不自动回答登录问题。换客户端或宿主重启后不能假定绑定或页面仍有效。

每个目录自动生成：

| 文件 | 用途 |
| --- | --- |
| version.json | 本次实时源码 SHA、干净状态、安装匹配和文件数 |
| events.jsonl | 窗口、每次 pump、来源和浏览器阶段指标；私有保留 |
| steps.md | 每步 UTC、耗时及经过白名单筛选的进度 |
| result.json | worker 真正关闭且页面设置恢复后才写入退出结果 |
| results/ | 双轮核验和原子保存后的私有页面快照 |

返回到对话的状态仅包含固定进度，完整路径和结果事件留在本机。失败时取消并等待 worker 关闭、恢复临时页面设置；失败日志不会成为成功结果。不关闭共享浏览器。此编排没有修改各游戏双轮和身份实现。

## 离线复盘

```sh
python tools/analyze_collection.py --run RUN_DIRECTORY --compare BASELINE_DIRECTORY --output REPORT_DIRECTORY
```

单次分析省略 `--compare`。兼容旧实验目录的中文文件名，无硬编码 UID、样本角色数、日期或本机路径。报告输出 `analysis.json` 和 `analysis.md`：校验两游戏提交状态、两轮名单/身份事件、实际保存的快照，再计算来源次数、跨度和区间并集、交叠、真实启动间隔、窗口空档、分操作统计。比较时核对源码、身份、名单和详情；数据变化不能冒充同条件提速。失败证据返回非零码；这是采集证据检查器，不能证明日志来源真实性，也不替代游戏内实际双轮比较。

报告默认是**已有证据的离线重算**，并非重新抓取。嵌套阶段与并行跨度不能直接相加。首次安装/开发/回归不计入一次采集成绩；需要测这些环节时另行保存实际命令与墙钟时间。

## 证据位置与人工边界

`tests/test_client_collection.mjs` 覆盖等待登录、续跑、启动预算、无脱离调用的后台推进、错误收尾与预检失败不碰浏览器；`tests/test_collection_analysis.py` 用真实 CLI/规范化/存储生成合成快照，覆盖交叠算法、账号变化、缺双轮/身份/详情、失败退出和节流证据拒绝。原有 `tests/test_client_parallel.mjs` 继续用真实 worker 管道验证并发、节流、双轮、提交和取消。

浏览器工具初始化、前台工具调用交接、用户登录确认属于宿主或人工边界，由 Skill 固定操作。页面选择器改变仍需真实页面验收；代码测试和历史日志重算不应被报告成当前页面的新增验收。PR 创建/合并只在用户授权范围内执行，不属于每次抓取的自动步骤。
