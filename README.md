# Hermes Lark Streaming

[![PyPI](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Hermes](https://github.com/NousResearch/hermes-agent) Gateway 飞书流式卡片插件 — 基于 CardKit v2.0 的进程内流式消息卡片。

灵感来源于 [openclaw-lark](https://github.com/larksuite/openclaw-lark) 和 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)。

[English](README.en.md)

![](assets/cover.jpg)

---

## 功能

- **流式输出** — AI 回复实时显示在交互卡片中，打字机效果
- **流式卡片** — 按事件顺序在单张卡片内动态渲染思考、工具调用、回答内容
- **思考过程** — 显示模型的推理/思考内容
- **工具调用** — 实时展示工具调用状态和进度，含标准图标和结果/错误块
- **CardKit v2.0** — 使用飞书 CardKit 流式 API；卡片创建失败时交回 Hermes Gateway 默认回复
- **终态卡片** — 完成后展示完整结果，含 token 用量、耗时、上下文信息
- **卡片样式** — 可配置卡片 header、终态 compact footer summary 显示开关及正文字字大小
- **消息保护** — 消息被删除/撤回后自动终止更新，避免无效 API 调用
- **图片解析** — 自动识别 markdown 图片引用，下载上传后替换为飞书 img_key
- **中断处理** — 处理 `/stop` 命令和消息打断，展示中断状态卡片并自动开启新会话
- **Cron 卡片推送** — 定时任务结果以飞书卡片形式推送，保留 Markdown 渲染
- **后台任务卡片推送** — `/background`（`/btw`）任务完成后以卡片形式推送，支持话题内回复
- **多语言** — 卡片文本（状态、工具面板、思考标签等）内置中英双语，根据飞书客户端语言自动切换

---

## 卡片展示

插件按事件到达顺序在卡片内动态渲染思考、工具调用、回答元素，多轮对话内容按实际顺序展示。

当长对话或工具调用步骤过多导致卡片元素接近飞书 200 上限时，自动拆分为多张卡片：旧卡片封存（数据完整），新卡片继续输出，仅最后一张卡片带页脚。单个工具面板步骤过多时也会按步骤边界拆分。

![](assets/streaming.jpg)

---

## 运行要求

- Hermes `>= 0.23.1`，本次固定验收 v0.23.1 / `3b421578`，已配置飞书平台
- `Python >= 3.11`
- `lark-oapi >= 1.4.0` — 飞书/Lark 官方 Python SDK
- `PyYAML >= 6.0` — YAML 解析库
- 飞书应用权限：消息卡片（CardKit）读写、消息发送与回复、图片上传

---

## 安装

完整安装步骤见 [INSTALL.md](INSTALL.md)。

### 原生插件安装

使用官方插件管理器安装已审阅、已提交的本地项目：

```bash
hermes plugins install file:///absolute/path/hermes-lark-streaming --enable --yes-deps
```

旧版注入安装先按 [INSTALL.md](INSTALL.md) 清理本插件标记并备份回滚环境。
此分支仍保留旧清理器和回归样本，运行时通过官方平台注册，不再修改 Hermes 源码。

---

## 配置

在 `~/.hermes/config.yaml` 中添加：

```yaml
streaming:
  enabled: true
```

完整配置说明、默认值、Profile 作用域以及各参数对应功能见 [CONFIGURATION.md](CONFIGURATION.md)。README 只保留安装所需的最小示例；配置行为以插件代码为准。

### 凭据

常用凭据来源：

| 来源 | 变量 |
|------|------|
| 环境变量 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（或 `LARK_APP_ID` / `LARK_APP_SECRET`） |
| 配置文件 | `~/.hermes/config.yaml` 中的 `feishu` 或 `lark` 区段 |

完整的凭据解析顺序及 Hermes platform 兼容路径见 [CONFIGURATION.md](CONFIGURATION.md)。

```env
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

### 卡片样式

如需调整卡片外观，可从以下最小示例开始：

```yaml
streaming:
  enabled: true
  progress_mode: text
  width_mode: default
  header:
    enabled: false
  body:
    text_size: normal_v2
  footer:
    enabled: true
    text_size: notation
    fields:
      - [tokens, context, quota_reset, cache, reasoning, balance]
    show_label: false
  panel_expanded: false
display:
  platforms:
    feishu:
      show_tool_use: true
```

`streaming.footer.enabled` 控制终态 compact Run Details，`streaming.footer.fields` 控制 adaptive metrics 与展开详情的候选字段。折叠摘要只保留高价值信息，例如 `✅ 2m 2s · gpt-5.6-luna · 96%`；有效 GPT weekly quota、Context、Cache 最多两个并排展示为圆环指标，后续 detail 也按两列排列。GPT quota 的剩余额度在 summary，重置时间在 detail；明确自定义 fields 可隐藏未列出的指标。Tokens、Cache、Reasoning 等 usage 来自 Hermes 当前 turn 的 canonical provider metadata，provider 未返回的数据会自动跳过，不会使用 session 累计值冒充。详情默认折叠，不受 `panel_expanded` 控制，也不会在 streaming 阶段动态更新。Reasoning Panel 与 Tool Panel 的行为保持不变。详细配置和维护字段入口见 [CONFIGURATION.md](CONFIGURATION.md)。

---

## CLI 命令

`HERMES_PYTHON` 指向实际运行 Hermes 的 PM 环境 Python：

```bash
$HERMES_PYTHON -m hermes_lark_streaming verify
$HERMES_PYTHON -m hermes_lark_streaming status
$HERMES_PYTHON -m hermes_lark_streaming migrate-config        # 查看配置差异
$HERMES_PYTHON -m hermes_lark_streaming migrate-config --apply
hermes plugins disable hermes-lark-streaming
hermes plugins remove hermes-lark-streaming
```

安装、更新和卸载由官方插件管理器处理；`legacy-clean` 仅用于删除旧版注入标记。
完整迁移证据见 [原生兼容性验收](docs/hermes-v0.23.1-native-compatibility.md)。

---

## 工作原理

`register(ctx)` 注册继承内置 `FeishuAdapter` 的适配器。官方流式快照与最终结果驱动正文，
工具、推理、usage、交互及停止事件映射到原有控制器。继续沿用现有卡片构建与状态管理。
后台复盘以官方工具回执保存实际变更，调用宿主汇总函数，将跨轮结果合并更新到最新卡底。
投递确认后才清除待处理结果，支持失败重试与重启恢复。

**消息处理流程：**

```
用户发送消息
  → 创建卡片会话
  → 流式更新（工具状态、文本增量 — 节流调度）
  → 图片 URL 异步解析替换
  → 终态卡片（token/耗时/上下文）
```

若消息被删除/撤回，自动终止后续更新。

**中断处理：**

- `/stop` 终止 — 用户主动停止，卡片展示中断状态：

![](assets/abort.jpg)

- 消息打断 — 用户发送新消息打断正在处理的回复，旧卡片展示中断状态，并自动为新消息创建新的流式卡片：

![](assets/interrupt.jpg)

---

## 注意事项

- 原生运行时不修改 Hermes 源码；旧注入残留会阻止加载
- Hermes 更新前需通过固定版本的真实宿主契约与全量回归
- 插件与 Hermes 内置飞书适配器互补工作：插件负责流式卡片，内置适配器负责消息收发
- 仅对飞书平台生效，其他平台不受影响

## 贡献者

感谢以下贡献者的 Issue 和 PR：

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

---

## 许可证

[MIT](LICENSE)
