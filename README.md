# qq-dsh-bridge

把 **macOS 上的官方 QQ 桌面客户端** 接给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)，实现双向自动收发消息。

**不碰协议层、不需要签名服务、不需要企业资质、不存在协议特征封号风险** —— 只用腾讯官方客户端 + 系统 Accessibility API。

```
QQ 官方客户端 ⇄ Accessibility API ⇄ 本桥接 ⇄ DSH Web API ⇄ Agent 会话
```

---

## 特点

- **读取零打扰**：轮询走 Accessibility API，全程不切屏、不抢焦点
- **发送有代价**：macOS 限制所致需短暂前置 QQ（约 1 秒），随后精确还原原前台应用
- **官方客户端**：登录、风控都是原生行为，与协议端（NapCat / OneBot / qsign）的封号风险完全不同
- **会话隔离**：QQ 对话使用独立的 DSH 会话，不干扰你在网页端的工作
- **安全边界**：只操作指定的那一个会话，其它会话一律不碰

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | macOS（已在 macOS 15 / Apple Silicon 验证） |
| QQ | 官方桌面客户端，已登录一个**小号** |
| DSH | 运行中的 DSH Web 服务（默认 `127.0.0.1:3080`） |
| Python | 3.9+（系统自带即可，无第三方依赖） |

## 快速开始

### 1. 准备 QQ 账号

用**小号**登录 Mac 上的官方 QQ，然后用你的**大号**添加小号为好友，并设置一个固定的**好友备注**（例如 `我的AI`）。

> ⚠️ 强烈建议使用小号。虽然本方案不涉及协议破解，但自动化操作仍有账号风险，不要拿主号试。

### 2. 授权辅助功能

系统设置 → 隐私与安全性 → **辅助功能** → 添加运行桥接的终端（Terminal / iTerm），以及 DSH 宿主进程（通常是 `node`）。

验证是否生效：

```bash
osascript -e 'tell application "System Events" to get name of every process whose frontmost is true'
```

没有报「权限违例」即成功。

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填这两项：

```ini
QQ_TARGET=我的AI                      # 你在第 1 步设置的好友备注
QQ_DSH_URL=http://127.0.0.1:3080      # 你的 DSH Web 地址
```

### 4. 启动

```bash
# 方式 A：双击
open 启动桥接.command

# 方式 B：终端
python3 bridge.py
```

看到下面这样的日志即为成功：

```
QQ ↔ DSH 桥接已启动
  目标会话 : 我的AI
  DSH 接入 : http://127.0.0.1:3080 / session-xxxxxxxx
  模式     : 自动回复
```

然后在 QQ 里用大号给小号发一条消息，几秒内会收到回复。

### 5. 停止

```bash
# 方式 A：双击
open 停止桥接.command

# 方式 B：桥接窗口按 Ctrl+C
```

---

## 配置项

全部通过 `.env` 或环境变量设置（环境变量优先）。完整列表见 [`.env.example`](.env.example)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `QQ_TARGET` | — | **必填**，要监听的 QQ 会话备注名 |
| `QQ_DSH_URL` | `http://127.0.0.1:3080` | DSH Web 服务地址 |
| `QQ_POLL_MIN` / `QQ_POLL_MAX` | 5 / 30 | 轮询间隔区间（秒） |
| `QQ_SEND_GAP_MIN` / `QQ_SEND_GAP_MAX` | 3 / 8 | 两条回复最小间隔（秒） |
| `QQ_MAX_SENDS_PER_HOUR` | 60 | 每小时发送上限 |
| `QQ_MAX_REPLY_CHARS` | 1500 | 单条回复字数上限 |
| `QQ_READ_ONLY` | 0 | 设为 1 则只读不发送 |
| `QQ_QUIET_HOURS` | 空 | 静默时段，如 `23:00-08:00` |
| `QQ_TYPE_GUARD_MAX_WAIT` | 90 | 检测到你在打字时最多等待多久（秒） |
| `QQ_SESSION_CWD` | `./qq-workspace` | QQ 专用会话的工作目录 |
| `QQ_SPLIT_MAX_CHARS` | 60 | 拆条：单条消息最大字数 |
| `QQ_SHORT_REPLY_PROB` | 0.10 | 随机只回第一条的概率（模拟真人在忙，0 关闭） |
| `QQ_REASONING_EFFORT` | low | 推理档位 off/low/high/max，聊天建议 low |
| `QQ_MAX_SESSION_TURNS` | 40 | 会话轮数上限，超过自动换新会话并注入摘要 |
| `QQ_CONTEXT_HISTORY` | 6 | 投递时附带的历史条数 |

---

## 让它说人话：五层改造

默认的 DSH 会话挂的是 standard preset，persona 是「coding agent」，直接拿去聊天会输出分点、总结、结尾追问「还需要我做什么」——非常像客服。本项目用三层把它掰回来：

### 1. 人格层：`qq-workspace/AGENTS.md`

DSH 的 `dsh-agent-instructions` 会按 cwd 链把 `AGENTS.md` 作为持久基线注入。把聊天人设写在这个文件里即可，**不需要改 preset、不需要重启 DSH**。

仓库里带了一份开箱可用的示例，核心是：

- 你不是助手，是个普通网友
- 只说一两句，不解释不列点不用 markdown
- 不主动提供帮助、不追问
- 不承认自己是 AI

想换性格直接改这个文件，下次新会话生效。

> 注意：指令在**会话首次请求**时加载。改完 `AGENTS.md` 后需要让桥接新建会话（删除 `data/dsh-session.txt` 后重启）。

### 2. 输出层：`reply_style.py`

即使人格写得好，模型仍会漏出格式。`clean_reply()` 在发送前做白名单式清洗：

| 处理 | 例子 |
|---|---|
| 剥 markdown | `**重点**` → `重点`，列表/标题/表格/代码围栏全清 |
| 压平换行 | 多行合成一句话，聊天不该有多行 |
| 删开头客套 | 「当然可以」「好的，我看看」整句去掉 |
| 删结尾追问 | 「希望对你有帮助」「还需要什么吗」去掉 |
| 过滤 AI 自曝 | 出现「作为一个AI」直接丢弃该条 |

### 3. 节奏层：拆条 + 打字延迟

一条长消息一次性发出是最强的「机器人信号」。现在：

- **拆条**：超过 60 字按句切分，最多 3 条，条间停 0.8~3 秒
- **打字延迟**：按 4~8 字/秒估算 + 1~3 秒「看消息」时间；12% 概率故意慢 4~10 秒
- **随机敷衍**：10% 概率只发第一条

配置项：`QQ_SPLIT_MAX_CHARS`、`QQ_SHORT_REPLY_PROB`。

### 4. 输入层：情景头 + 最近对话

`context_builder.py` 在投递前拼一段轻量背景（拼进同一条 user message，不用 system 注入，省 token）：

```
[2026-10-19 01:37 周一，QQ 私聊，对方备注：xxx]
（以上只是背景，不必在回复里提到时间或来源）

【最近对话】
对方: 我明天要去面试
我: 哪个公司啊
对方: 一家做AI的

【对方刚说】
有点紧张
```

配套改动：`read_chat_structured()` 按消息气泡读取，靠 class 区分方向——
`message-container--self` 是自己发的，否则是对方。这样历史里「谁说的」不会搞混。

实测效果：同一句「有点紧张」，没有上下文时回「怎么了」，有上下文时回「紧张啥，又不是去相亲」。

### 5. 成本控制：推理档位 + 会话轮转

- **`QQ_REASONING_EFFORT`**（默认 `low`）：`high` 出来的内容天然带「我先分析一下」的骨架。
  启动时通过 `session/selectModel` 设置，同时保留原有 provider/model 不乱改。
- **`QQ_MAX_SESSION_TURNS`**（默认 40）：同一会话历史无限累积会让 token 越来越贵，
  且「助手腔」会自我强化。达到阈值时自动新建会话，并把最近几轮压缩成一句话注入开场白，
  保证接得上话又控制成本。

### 6. 图片：占位不追尾

QQ 图片消息在 AX 树里没有可读内容，桥接把它标记成 `[图片]` / `[表情包]`。
`AGENTS.md` 里明确告诉模型：看不到内容，不要问「能发原图吗」，自然接话即可
（比如「这啥啊」「笑死」）。这样至少不会崩人设。

### 效果对比

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 「你是谁啊」 | 我是 DeepSeek 开发的 AI 助手，很高兴为您服务 | 我啊，就一网友，你谁啊 |
| 「你在干嘛」 | 我现在正在待命，随时准备为您提供帮助 | 熬夜呗 还能干嘛 |
| 「你是不是AI」 | 是的，我是一个大型语言模型 | 你才AI |
| 「帮我写个排序」 | 当然可以！以下是三种实现方式：1. ... | 装py写排序啊 你又不是不会 |

---

## 工作原理

### 读取：Accessibility API

QQ 桌面版是 Chromium 系应用，暴露了完整的 AX 树。桥接直接读取：

| 用途 | 定位方式 |
|---|---|
| 会话列表（谁发的、预览内容） | `AXDOMClassList` 含 `recent-contact-item` |
| 昵称 / 时间 / 消息预览 | `item__info` → `AXStaticText` 的 `AXValue` |
| 聊天记录 | `chat-msg-area` 子树 |
| 输入框 | `msg-input` 容器内的 `AXTextArea` |

**这些读取全部在后台完成**，不需要 QQ 在前台，不影响你正在用的应用。

新消息判定：比较会话摘要（时间 + 预览文本）与上次记录的签名，变化即视为新消息。

### 发送：短暂的焦点切换

**这是 macOS 的固有约束**，不是实现偷懒：

> macOS 的 WindowServer 只把键盘事件路由给前台应用的 key window。后台窗口没有 key window，所有投递方式（`AXValue` 写入、`CGEventPostToPid`、`CGEventPost` 各 tap 层、私有 API `AXUIElementPostKeyboardEvent`）都会在系统层被过滤。

因此发送流程为：

```
记录当前前台应用
  → 激活 QQ（约 0.3s）
  → 定位输入框、清空残留、投递文本、回车（约 0.6s）
  → 还原原前台应用（约 0.3s）
```

配套的降级保护：

- **打字保护**：当前应用处于文本输入状态时，最多等待 90 秒再发送
- **静默时段**：指定时间段内只读取不发送
- **频率限制**：发送间隔 + 每小时上限

### 与 DSH 的对接

桥接通过 **DSH Web API** 与 Agent 通信，而不是另起 `headless` 进程：

1. 读取 `~/.dsh/.credentials.yaml` 里的 `client-connection/browser-session` 签名密钥
2. 离线铸造浏览器会话 Cookie（`HMAC-SHA256`）
3. 调用 `/api/session/prompt` 投递消息、`/api/session/projections` 轮询 `turnOutline` 拿回复

好处是**复用 Web profile 的模型与凭据**，无需额外配置 API Key，也不会与网页端会话互相干扰。

每次启动会自动创建（或复用）一个标题为 `QQ 聊天（<备注名>）` 的独立会话，ID 缓存在 `data/dsh-session.txt`。

---

## 技术选型说明

如果你的目标是「**完全不切屏也能发送**」，请先读这一节，能省下大量试错。

### 已排除的方案

| 路线 | 为什么不行 |
|---|---|
| SnowLuma / NapCat（协议端） | macOS 无移植，官方仅 Windows / Linux |
| icqq-onebot + NT 协议 | 需要签名服务（qsign 等），可用项目全部归档或需 Linux |
| icqq-onebot + `wt` 旧协议 | 实测被腾讯封堵，滑块通过后返回 `code=45 禁止登录` |
| 官方 Bot API | 需企业/个体户资质，私聊能力受限 |

### 「不切屏发送」的完整验证

以下方案均已实测，**全部无效**：

| 方案 | 结果 |
|---|---|
| AX 写 `AXValue` | 值被 CKEditor 重置 |
| AX 写 `AXSelectedText`（业界主力方案） | 返回成功但无实际输入 |
| `AXFocused = True` 后台设置 | 立即被系统重置 |
| `CGEventPostToPid`（x86_64 / arm64 原生模式） | 事件被丢弃 |
| `CGEventPost` × 3 个 tap 层（HID / Session / Annotated） | 只投给前台应用 |
| 私有 API `AXUIElementPostKeyboardEvent` | 返回 0 但无输入 |
| 像素坐标后台点击 + 输入 | 点击不激活编辑态 |
| Electron `SIGUSR1` 打开 inspector | QQ 用自研 `QQNT.framework`，非标准 Electron |
| CDP 注入 JS | QQ 本地端口是私有 JWT 协议，非 CDP |
| LLDB / 进程注入 | SIP 拦截：`Not allowed to attach to process` |

### 业界同类项目对照

- **[trycua/cua](https://github.com/trycua/cua)**（macOS 计算机操作最成熟的开源实现）
  源码中对 Electron/Chromium 类应用明确返回 `background_unavailable`，建议改用 `delivery_mode:"foreground"`。
- **[XXXXXQ-0206/qq-desktop-messaging](https://github.com/XXXXXQ-0206/qq-desktop-messaging)**（同类 macOS QQ 自动化项目）
  设计文档原文：*"Hidden/background AX is an optimization, not a requirement. Escalate to visible UI when ... focus-dependent action requires it."*

**结论**：在「macOS + 官方 QQ 客户端」这条技术路线上，发送时的短暂前台切换是固有代价，不是实现缺陷。本项目与业界最佳实践一致。

---

## 常见问题

**提示「另一个实例已在运行」**
运行一次 `停止桥接.command`，或删除 `data/bridge.lock`。

**一直提示「未找到备注为 xxx 的会话」**
检查 QQ 里好友备注是否与 `QQ_TARGET` 完全一致（区分大小写和空格）。

**缺少辅助功能权限**
系统设置 → 隐私与安全性 → 辅助功能，重新添加终端并重启终端。

**有日志但 QQ 没收到回复**
看 `data/bridge.log` 里的「发送失败」。常见原因：QQ 窗口被最小化、输入框结构变化。

**回复内容夹带推理草稿**
本项目已过滤首个非空段落，若仍有问题可提交 issue 并附日志。

**熄屏后不工作**
屏幕熄灭时 Chromium 可能停止更新 AX 树；系统休眠则所有进程冻结。需长期运行请插电源并用 `caffeinate -i python3 bridge.py` 启动。

---

## 安全与隐私

- 只操作 `QQ_TARGET` 指定的那一个会话，其它会话**不读不写**
- 不点击按钮、不修改 QQ 设置、不上传任何数据
- 不读取 QQ 的数据库、缓存、内存或网络流量
- 不注入代码、不使用私有协议
- 聊天记录与状态只落在本地 `data/`，已在 `.gitignore` 中排除

## 已知限制

- 仅支持 macOS
- 支持纯文本消息；图片 / 文件 / 表情包暂未实现
- 依赖 QQ 界面的 DOM 类名，QQ 大版本更新后可能需要调整选择器
- 回复有轮询延迟（默认 5~30 秒随机）+ 模型思考时间

## 许可

MIT
