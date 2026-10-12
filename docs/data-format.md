# 数据格式和完整度

格式权威是发行包的 `src/hoyo_roster/contracts/schemas/`；验证器同时验证 schema、跨文件一致性、完整度、规范 JSON 和 roster/build 一一对应。`data/manifest.json` 列出三游戏文件。每个身份的当前 generation 都有三游戏基线，只有所选游戏包含个人数据；其他游戏始终 `not_synced` 且为空。

Profile 保存游戏、UID、server、nickname、player_level 和 sync（state/source/synced_at）。名单的字符串角色 ID 是 map key；Build 为 `builds/<正整数角色ID>.json`，不重复存储角色身份。百分比为比例值（例如 5% → 0.05），精确数字通过 Decimal 与规范序列化保留。只保存当前装备，不保存库存、货币、资源、目标配装、推荐结果或公共游戏机制数据库。

| 游戏 | 正式状态 | 来源和完成条件 | 缺失行为 |
|---|---|---|---|
| 原神 | `partial` | 本人官方 Binding + 计算器页面自然请求的单页完整角色列表；精确 UID/区服/请求字段，total 和 size 证明闭合 | 未被来源提供的字段保持不可用；部分/未知多页契约拒绝，不能冒称 complete |
| 绝区零 | `complete` | 官方 Binding、战绩角色名单与养成 owned 名单严格相等；逐角色详情与批量详情恰好覆盖名单，邦布名单闭合；两次完整度证据相同 | 未知分页、重复/遗漏 ID、来源不一致、缺少必需属性失败；schema 明确允许的可选值可为 null |
| 星铁 | `partial` | 官方 Binding、页面会话初始化与设备头证据、封闭页面、两次真实来源观察一致；有 owned roster 时采集详情 | owned 标记不可用/全为 false 时只更新 Profile，保留此前 roster/build；空基线保持空名单。旧角色值可能未刷新 |

原神保留来源支持的等级、突破、命座、好感、技能和当前武器等有限字段；不会补造面板或圣遗物。绝区零包括代理人等级、晋阶、影画、技能、音擎、驱动盘、面板和邦布；`complete` 指已实现来源和 schema 的闭合，不表示游戏账号所有信息。星铁包括来源确证的角色等级、技能、当前光锥、遗器与面板子集；缺少的推荐面板源不等于授权生成推荐配装。

两次采集比较业务数据和来源覆盖，发现漂移就停止；生命周期拒绝不允许的身份变化、名单缩小、进度回退或字段覆盖回退。HSR 旧值保留遵循现有 materialize/lifecycle 契约，不能把 Profile 新时间戳理解为全体角色均新鲜。禁止增加逐字段 coverage 数据；来源诊断只在内存中用于验证。

`not_synced` 是未收集基线，不能把空名单当作已收集的空账号。`synced_at` 是本次保存业务快照的 UTC 时间，无变化时保持此前值。正式数据通过 candidate 验证和完整安装验证后才成为 current。
