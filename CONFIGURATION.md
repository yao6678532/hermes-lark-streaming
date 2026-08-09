# Hermes Lark Streaming Configuration

本文档描述当前版本实际支持的配置项。

配置行为以 `hermes_lark_streaming/config.py` 为最终依据。历史 `.plans` 文档属于已完成实施计划，不应作为配置参考。

## 配置文件位置与 Profile

Hermes 配置是 profile scoped。默认 profile 使用：

```text
~/.hermes/config.yaml
```

命名 profile（例如 `assistant`）使用：

```text
~/.hermes/profiles/assistant/config.yaml
```

修改 `~/.hermes/config.yaml` 不会自动修改 `assistant` profile。如果 gateway 使用 `assistant` profile，就必须修改该 profile 对应的 `config.yaml`。插件会使用 Hermes 为当前消息解析出的 profile home 创建对应的 `Config` 实例，因此多 profile 可以拥有不同的插件配置和凭据。

本文不列出未经当前 Hermes CLI 验证的 profile 管理命令；直接编辑当前 gateway 实际使用的 profile 配置文件即可。

## 推荐完整配置示例

下面展示插件配置的完整结构。示例中的 `streaming.enabled: true` 是启用示例，不是默认值；注释中的默认值以当前代码为准。

```yaml
streaming:
  enabled: true                 # 插件默认 false
  reasoning_mode: segmented     # segmented / merged；默认 segmented
  progress_mode: text           # text / card；默认 text
  panel_expanded: false         # 默认 false
  card_ttl_sec: 600             # 默认 600 秒
  width_mode: default           # default / compact / fill；默认 default

  header:
    enabled: false              # 默认 false

  body:
    text_size: normal_v2        # 默认 normal_v2

  footer:
    enabled: true               # 默认 true
    text_size: notation         # 默认 notation
    fields:
      - [status, elapsed, context, model]
    show_label: false           # 默认 false

display:
  platforms:
    feishu:
      show_reasoning: false     # 默认 false
      show_tool_use: true       # 默认 true
      show_tool_detail: true    # 默认 true；只控制每条工具的 secondary detail 行
      tool_detail_mode: full    # full / compact；默认 full
      clarify_style: text       # text / card；默认 text
      confirmation_style: hermes # hermes / openclaw；默认 hermes

agent:
  gateway_notify_interval: 60   # 示例；Hermes-owned，不是插件默认值
```

`agent.gateway_notify_interval` 属于 Hermes Gateway，不属于插件。上例中的 60 只是示例值，不是插件默认值；若省略它，默认值由当前 Hermes 版本决定。

## Feishu / Lark 凭据与 endpoint

插件读取凭据时，优先使用一组完整的 secret 环境变量；只有 app ID 和 app secret 都能读取到时，环境变量来源才会整体优先于 YAML 配置。变量别名按以下顺序读取：

1. `FEISHU_APP_ID`，否则 `LARK_APP_ID`
2. `FEISHU_APP_SECRET`，否则 `LARK_APP_SECRET`

在 Hermes 环境中，这些值通过 Hermes secret scope 读取；未加载 Hermes secret scope 时回退到进程环境变量。单 profile 通常由该 profile 的 `.env` 提供 secret，具体以 Hermes 的 secret 加载方式为准。

当使用环境变量凭据时，endpoint 按以下顺序读取：`FEISHU_BASE_URL`、`LARK_BASE_URL`、`https://open.feishu.cn`。插件代码没有把 `FEISHU_DOMAIN` 当作自己的配置项。

没有完整环境变量凭据时，配置文件来源按以下顺序查找：

1. 顶层 `feishu`，再到顶层 `lark`；两者均支持 `app_id`、`app_secret`、`base_url`。
2. `gateway.platforms.feishu.extra` / `gateway.platforms.lark.extra`。
3. `platforms.feishu.extra` / `platforms.lark.extra`。

嵌套 Hermes platform 配置也会读取 platform 或 `extra` 中的 `base_url`。当嵌套配置的 platform 是 `lark` 且没有明确 `base_url` 时，`domain: lark` 会映射到 `https://open.larksuite.com`。建议 Lark 配置显式写出 `base_url`，避免依赖路径差异：

```yaml
lark:
  app_id: cli_xxxxx
  app_secret: xxxxx
  base_url: https://open.larksuite.com
```

配置文件中的凭据属于当前 profile；修改后建议重启 gateway，使插件和 Hermes adapter 一起重新建立连接。

## 功能 → 参数速查表

| 功能 | 配置路径 | 类型 / 可选值 | 默认值 | 生效方式 | 说明 |
|---|---|---|---|---|---|
| 启用流式卡片 | `streaming.enabled` | bool | `false` | 重启 gateway | 是否启用插件流式卡片；关闭时交回 Hermes 原生发送。 |
| reasoning 组织方式 | `streaming.reasoning_mode` | `segmented` / `merged` | `segmented` | 重启 gateway | `segmented` 按事件顺序保留多个 reasoning 段；`merged` 在完成卡片中合并 reasoning。 |
| 是否显示 reasoning | `display.platforms.feishu.show_reasoning` | bool | `false` | 运行时重新读取 | 只控制 reasoning 是否展示，不改变 `reasoning_mode`。也兼容 `display.show_reasoning` 作为全局 fallback。 |
| 是否显示工具调用 UI | `display.platforms.feishu.show_tool_use` | bool | `true` | 运行时重新读取 | 控制统一 Tool Panel 是否显示；`false` 只隐藏面板，不影响 Hermes tools 真实执行。也兼容 `display.show_tool_use` 作为 fallback。 |
| 工具详情 | `display.platforms.feishu.show_tool_detail` | bool | `true` | 运行时重新读取 | 只隐藏或显示每条工具 step 的 secondary detail 行；不隐藏 Tool Panel、标题、状态、耗时或 result/error diagnostics。也兼容 `display.show_tool_detail` 作为 fallback。 |
| 工具详情模式 | `display.platforms.feishu.tool_detail_mode` | `full` / `compact` | `full` | 运行时重新读取 | `full` 保留完整的 sanitized detail；`compact` 在同一 sanitized detail 基础上对 Terminal/command 优先显示 executable/script 和安全 subcommand，隐藏长参数 payload；其他工具保守使用 sanitized detail。非法值 fallback 到 `full`。也兼容 `display.tool_detail_mode` 作为 fallback。 |
| Clarify presentation | `display.platforms.feishu.clarify_style` | `text` / `card` | `text` | 运行时重新读取 | `text` 使用 Hermes 原生文本交互；`card` 使用 Feishu Clarify Card；非法值 fallback 到 `text`。 |
| Approval presentation | `display.platforms.feishu.confirmation_style` | `hermes` / `openclaw` | `hermes` | 运行时重新读取 | `hermes` 保留 Hermes 原生 approval presentation；`openclaw` 使用插件 Approval Card presentation。Hermes approval state / resolver 仍是真实 source of truth；非法值 fallback 到 `hermes`。 |
| Working 显示位置 | `streaming.progress_mode` | `text` / `card` | `text` | 重启 gateway | `text` 保持 Hermes 原生 long-running heartbeat 独立文本消息；`card` 在活动 streaming card 能安全接管时显示在卡片底部 progress/loading 区域。插件不解析 `Working` 字符串、不启动本地 heartbeat ticker，也不存在 `streaming.progress_interval` 或 `streaming.heartbeat_interval`。 |
| Hermes heartbeat 周期 | `agent.gateway_notify_interval` | number，单位秒 | 由当前 Hermes 版本决定 | 重启 gateway | **Hermes-owned configuration**。控制 Hermes Gateway long-running heartbeat 周期，不是插件 timer；`streaming.progress_mode` 只决定显示位置。profile scoped。 |
| 卡片宽度 | `streaming.width_mode` | `default` / `compact` / `fill` | `default` | 重启 gateway | 非法值 fallback 到 `default`。 |
| 完成态面板展开 | `streaming.panel_expanded` | bool | `false` | 重启 gateway | 控制完成卡片中的 reasoning / unified Tool Panel 是否保持展开；默认 `false`，因此工具面板完成时折叠。 |
| 卡片存活检测 | `streaming.card_ttl_sec` | int 秒 | `600` | 重启 gateway | 控制卡片 session 的存活检测时长；代码会将值转换为 int。 |
| 卡片 header | `streaming.header.enabled` | bool | `false` | 重启 gateway | 控制 streaming / completed / error header；状态会自动使用蓝、绿、红主题。 |
| answer body 文字大小 | `streaming.body.text_size` | CardKit text size 字符串 | `normal_v2` | 重启 gateway | 传给 answer markdown 的 `text_size`；插件只在缺失或空值时回退到 `normal_v2`。 |
| 完成态 Run Details | `streaming.footer.enabled` | bool | `true` | 重启 gateway | 控制完成卡片中默认折叠的 Run Details；保持 `footer` 配置名兼容。 |
| Run Details 文字大小 | `streaming.footer.text_size` | CardKit text size 字符串 | `notation` | 重启 gateway | 传给 Run Details 标题与展开内容；插件只在缺失或空值时回退到 `notation`。 |
| Run Details 字段布局 | `streaming.footer.fields` | `list[list[str]]`；一维 list 也接受 | `[[status, elapsed, context, model]]` | 重启 gateway | 每个子数组是一行，控制展开内容。支持字段见下表；空值、缺失或非 list 使用默认布局。 |
| Run Details 标签兼容项 | `streaming.footer.show_label` | bool | `false` | 重启 gateway | 保留旧配置项；Run Details 展开内容始终使用清晰的字段标签。 |

### `footer.fields` 支持的字段

这些名称由当前 `cardkit/builder.py` 的 `_render_footer_field()` 实际支持：

| 字段 | 含义 |
|---|---|
| `status` | 完成、错误或中止状态。 |
| `elapsed` | 本次运行时长。 |
| `model` | 使用的模型名。 |
| `tokens` | 输入 / 输出 token 数。 |
| `context` | 已用 / 最大 context window 及百分比。 |
| `balance` | Hermes runtime 提供的余额字符串（若有）。 |
| `gpt_quota` | Hermes / credential pool 提供的 GPT/Codex quota 字符串（若有）。 |

未知字段不会渲染。GPT/Codex quota 是运行时数据展示，不属于本次配置改造；它也不代表新增了 quota 配置项。

## 默认值与非法值 fallback

当前代码采用保守 fallback 的主要规则如下：

- `reasoning_mode` 缺失、为空或非法时为 `segmented`。
- `progress_mode` 缺失、为空或非法时为 `text`。
- `clarify_style` 缺失、为空或非法时为 `text`。
- `confirmation_style` 缺失、为空或非法时为 `hermes`。
- `tool_detail_mode` 缺失、为空或非法时为 `full`。
- `width_mode` 缺失、为空或非法时为 `default`。
- `enabled`、`panel_expanded`、`header.enabled`、`show_reasoning` 缺失时为 `false`；`footer.enabled`、`show_tool_use` 和 `show_tool_detail` 缺失时均为 `true`。这些配置应使用 YAML bool；代码对值采用 Python `bool()` 转换。
- `body.text_size` 缺失或空值时为 `normal_v2`；`footer.text_size` 缺失或空值时为 `notation`。这两个 text size 字符串不是插件枚举，非法的 CardKit 值不会由插件额外改写。
- `footer.fields` 缺失、空 list、非 list，或 footer 不是 mapping 时使用 `[[status, elapsed, context, model]]`；一维字段 list 会自动包装为一行。直接传入空字段 list 时保持既有 builder 语义，不渲染详情。`footer.show_label` 保留读取兼容，但 Run Details 始终显示字段标签。
- `card_ttl_sec` 缺失时为 `600`。代码会调用 `int()`，因此不可转换的非数字值不是 fallback，而会在读取时失败。

## 热加载 vs Gateway restart

`Config._reload()` 每次从当前 profile 的配置文件读取、不更新缓存。以下 display presentation 配置项在每次访问时重新读取，通常无需因为配置值本身重启 gateway：

- `display.platforms.feishu.show_reasoning`
- `display.platforms.feishu.show_tool_use`
- `display.platforms.feishu.show_tool_detail`
- `display.platforms.feishu.tool_detail_mode`
- `display.platforms.feishu.clarify_style`
- `display.platforms.feishu.confirmation_style`

普通 `streaming.*` 通过缓存的 `Config._load()` 读取，包括：

- `streaming.enabled`
- `streaming.reasoning_mode`
- `streaming.progress_mode`
- `streaming.panel_expanded`
- `streaming.card_ttl_sec`
- `streaming.width_mode`
- `streaming.header.*`
- `streaming.body.*`
- `streaming.footer.*`

Streaming 过程中不渲染 Run Details；统一 Tool Panel 在存在 active tool 时自动展开；进入 answer 且没有 running tool 时自动折叠。answer 后再次开始工具调用会重新展开。一个 physical card 默认只创建一个 Tool Panel；当元素接近 CardKit 阈值时仍会按 tool step 边界拆卡。Run Details 只在最终 physical card 完成时显示，split card 的中间 seal card 不重复显示。

Run Details 使用现有 CardKit `collapsible_panel`，默认 `expanded: false`。其 summary 只显示 `运行详情 · {elapsed} · {model}`（英文为 `Run Details · {elapsed} · {model}`），缺失值不会产生多余分隔符；展开后沿用 `footer.fields` 与现有 footer metadata。它不读取 `panel_expanded`，因此不会改变 Reasoning Panel 或 Tool Panel 的展开语义。

修改这些 `streaming.*` 项后建议重启 gateway，确保新的 `Config` 实例加载配置。凭据和 profile 路径变化也建议重启 gateway。`agent.gateway_notify_interval` 是 Hermes 自身在 gateway 运行配置中读取的参数，修改后应重启 gateway。

## 常见配置任务

```yaml
# 隐藏工具调用 UI；不会禁止工具执行
display:
  platforms:
    feishu:
      show_tool_use: false

# 保留 Tool Panel 的标题、状态、耗时，但隐藏每条工具的 secondary detail 行
display:
  platforms:
    feishu:
      show_tool_use: true
      show_tool_detail: false

# Terminal/command detail 使用语义压缩（不是简单字符串截断）
display:
  platforms:
    feishu:
      show_tool_use: true
      show_tool_detail: true
      tool_detail_mode: compact

# 显示 reasoning
display:
  platforms:
    feishu:
      show_reasoning: true

# 合并 reasoning 展示
streaming:
  reasoning_mode: merged

# 启用 Clarify Card
display:
  platforms:
    feishu:
      clarify_style: card

# 启用 Approval Card presentation
display:
  platforms:
    feishu:
      confirmation_style: openclaw

# 把 Working 状态放进流式卡片
streaming:
  progress_mode: card

# 把 Hermes Working heartbeat 改成 60 秒 / 30 秒
agent:
  gateway_notify_interval: 60
# 或：agent.gateway_notify_interval: 30
```

heartbeat 周期是 Hermes 参数，不是 `streaming.progress_mode`。修改 `agent.gateway_notify_interval` 后需要重启 gateway。

## Phase 1～4 保留的关键语义

- Phase 1：`show_reasoning` 控制是否显示，`reasoning_mode` 控制组织方式；reasoning 与 answer/body 分离。GPT/Codex quota footer 不属于本任务的配置改造。
- Phase 2：Clarify Card 只是 Feishu presentation，Hermes resolver / pending state 仍是 source of truth；卡片失败时回退到 Hermes text flow。
- Phase 3：`confirmation_style: openclaw` 只是 presentation style，不替换 Hermes approval state，不增加自定义 always-allow policy，最终仍使用 Hermes resolver。
- Phase 4：插件只消费真实 Hermes long-running notifications，不创建本地 ticker；heartbeat 周期由 `agent.gateway_notify_interval` 控制，iteration 来自 Hermes structured activity（例如 `api_call_count`），不从文本解析。无法安全接管 card 时 fail-open。

## Fail-open 语义

这些配置是 UI enhancement，不应破坏 Hermes 核心行为：

- streaming card 创建失败时交回 Hermes fallback。
- progress card 无法安全拥有 heartbeat 时使用 Hermes text heartbeat。
- Clarify Card 不支持或发送失败时回到 Hermes text flow。
- Approval presentation 不可用时保留 Hermes 原生 approval。
- 非法配置值优先采用保守 fallback；代码没有把这些 UI 配置变成 Hermes policy。
