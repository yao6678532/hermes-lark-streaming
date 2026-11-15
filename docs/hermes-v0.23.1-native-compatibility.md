# Hermes v0.23.1 原生迁移验收

审计日期：2026-11-15。原生接入实现与隔离验收通过；本机切换结果另见升级记录。

## 固定版本与完整回归

- 旧插件：0.12.0，`8ee33871cb1fd8852f7f29b932f16d222902c124`。
- 旧 Hermes：v0.21.7，`71d34bad1045610252eda02ee79549436896e784`。
- 测试和升级目标：正式版 v0.23.1，`3b421578588243f920886e6fe79a6faa87ea026e`。
- 使用官方 PM 的隔离 Python 3.14.9 环境，临时 HOME/HERMES_HOME，无生产凭据。

| 验收 | 结果 | 证据 |
|---|---|---|
| 新宿主完整回归 | 1542 passed，无跳过，6 条既有警告 | hermes-v0.23.1-runtime-regression.json / .log |
| 真实原生平台注册与运行链 | 31 项通过 | hermes-v0.23.1-platform-audit.json |
| 后台复盘原生回执链 | 18 项通过 | hermes-v0.23.1-review-receipts-audit.json |
| ruff 全仓、mypy | 通过，47 个源文件 | 本地执行记录 |
| 网络尝试、宿主源码变化 | 0；源码及 tracked diff 未变 | 各审计报告 |

旧基线 1509 项测试已有 6 条警告：SDK 弃用 API 1 条；同一故障注入测试在五种
历史源码布局下产生 `FlushController._do_flush` 未 await 警告 5 条。此次数量未增加，
新增原生链探针没有协程生命周期警告。测试未发送真实飞书消息，未调用真实模型。
模型响应经官方 LLM middleware 本地脚本提供，SDK 边界模拟成功/失败。

## 接入与功能保留

`plugin.yaml` 与 `register(ctx)` 通过官方 `register_platform` 注册继承内置
FeishuAdapter 的适配器，平台仍为 `feishu`。连接、权限、配对、群聊门控和媒体继承内置
实现，不改宿主源码或全局对象。现有控制器、状态、卡片模板、图片处理和交互继续复用。

- 正文来自官方 edit transport 累积快照与最终回复；推理增量有完整 post-API 数据对账。
  不依赖可能延迟或被丢弃的观察事件累积正文，原始私有草稿不渲染。
- profile、入站消息、session、run、API iteration 与引用 anchor 分别关联，阻止串卡和晚到覆盖。
  API 请求结束不封整轮卡，澄清/审批暂停和最终完成分别处理。
- 保留首屏、推理/工具面板、续流、排队交接、停止/中断、长文本与自动拆卡、图片与媒体、
  Run Details、当前轮 usage/额度页脚、Clarify v2、审批、Agent Status、后台/Cron 卡片。
- 创建或终态更新失败回退内置最终回复一次，排队交接也不丢上一轮结果；原生状态通知保留。
- 原有 streaming/display/凭据用法保留；迁移只补必要的原生开关并记录差异，重复执行无变化。
- verify/status 区分注册 factory 和实际运行 adapter，并检查旧注入残留；禁用恢复内置 adapter。

31 项契约探针涵盖真实 AIAgent 工具循环、GatewayStreamConsumer、真实 pending human-input、
Clarify 多选、陈旧审批不影响新请求、真实双 profile 同名 ID 隔离、两个真实复盘、真实配置保存、
在线/离线 Cron 与媒体、失败重试、长文本及原生回退。回归另覆盖强制拆卡后每段内容恰好一次。

## 跨回合后台摘要

默认摘要链的 generation gate 缺口仍存在于宿主：旧轮复盘完成前新轮启动时，
`TurnRunner._make_bg_review_callbacks` 可在调用 adapter.send 前丢弃摘要。
原始对照报告 `hermes-v0.23.1-native-audit.json` 标记 blocked，是该默认路径的预期失败证据，
不表示已经实现的替代路线失败。

新路线注册官方 `post_tool_call`，读取真实 `background_review` write origin、归属上下文和
成功的 memory/skill_manage 结果，在 gate 之前写入 profile 私有 SQLite 回执日志。
调用宿主 `summarize_background_review_actions` 恢复原文案、通知设置和待审批提案语义，
旧轮与新轮待处理变更合并后交给原有 Agent Status 控制器更新最新卡片底部。
投递失败保留回执并重试，重启重放，确认显示成功才 ACK 对应批次；原生正常摘要去重，
不会覆盖合并结果或另发重复消息。不是把工具调用本身当作更新成功，也不生成新模型摘要。

真实 fork/工具循环测试中推进 generation 后，默认摘要到达 0 次，成功回执仍为 1。
集成探针执行两轮真实后台写入，验证合并、双投递失败保留、成功 ACK 与原生回调去重。
宿主 summarizer/origin 辅助函数为固定提交的只读依赖，未来升级必须再做契约检查。

## 真实启动顺序补充

正式启动前的检查发现，默认 10 秒加载线程下，如果 gateway.run 先触发插件发现，
插件再导入同一 GatewayRunner 会等待主线程的 import lock。已将宿主路径定位改为
已加载的 hermes_cli.plugins facade，去掉注册和诊断中的循环导入。新增契约使用
真实 gateway-first 导入和默认有超时的线程加载，不再以 load_timeout_seconds=0 绕过。
31 项探针包含这一启动顺序，完整回归在修复后重新运行。

## 安装与复现

使用目标完整 PM 环境执行：

```bash
$HERMES_PYTHON scripts/verify_native_compatibility.py --source <clean-pinned-host> --probe platform --report <report>
$HERMES_PYTHON scripts/verify_native_compatibility.py --source <clean-pinned-host> --probe tool-receipts --report <report>
$HERMES_PYTHON scripts/verify_native_compatibility.py --source <clean-pinned-host> --probe regression --fixture-source <old-host-git-repo> --report <report>
```

探针临时目录隔离并拒绝网络连接，检查固定 SHA、宿主源哈希及全部 tracked diff，
记录插件源哈希并防止测试中途代码变化。退出 0 通过，1 执行失败，2 功能门禁未满足。
`--probe baseline` 仅复现默认摘要缺口，预期退出 2。正式安装使用官方插件管理器，详见 INSTALL.md。

官方静态扫描为 caution：两个 high 项来自保留的 split_cron AST 编译迁移助手，
其余 medium/low 主要为历史源码测试样本；无 critical/dangerous 项。未绕过危险阻断。
历史 AST 代码仅保留标记清理与旧版回滚，不注册为新版运行路径。

## 参考实现

对照固定源码：Aowen `7a3c0ef` 使用官方适配器 send 识别通知，活跃卡内显示、否则独立文本；
Cheerwhy `76835fd` 使用官方平台注册，未关联活动轮时回退内置 sender；aiduPOP `a531023`
另有 callback monkey-patch，线性会话显示 Review 面板，其他场景原 sender。
这些项目未提供本项目跨轮、封卡后仍更新最新卡底的完整等价范例，故此次借鉴原生注册和
投递回退，保留本项目自己的展示与持久回执方案，不用额外文本替代原卡底设计。
