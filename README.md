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
