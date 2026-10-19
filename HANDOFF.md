# 会话交接文档 · qq-dsh-bridge

> 写于 2026-10-19 10:15。本文件把上一个会话的全部上下文压成一份可执行的状态记录，
> 新会话读它就能接着干，不必从头摸索。
>
> **先读「三、当前已知 Bug」——项目现在处于「功能齐但有一个重复回复的缺陷」的状态。**

**交接时的运行状态**：

- 桥接进程：**已停止**（日志末尾是 `10:12:08 桥接已退出`）
- 代码：能跑，但带第三节的 Bug 1
- 未提交改动：有（见第四节），**未 push**

---

## 一、项目是什么

把 **macOS 上的官方 QQ 桌面客户端**接入 DeepSeek Harness（DSH），实现「用另一个 QQ 号跟 AI 聊天」。

- 运行目录：`/Users/gusheng/Documents/deepseek-harness/默认工作区/qq-dsh-bridge`
- 远程仓库：https://github.com/Gusheng366/qq-dsh-bridge-MacOS （公开，MIT）
- 当前分支：`main`，本地与远程同步在 `58cca8a`（**之后还有未提交改动，见第二节**）

### 为什么不用协议端

这条路已经走过并全部堵死，**不要再回头试**：

| 方案 | 结论 |
|---|---|
| SnowLuma / NapCat | macOS 无移植（官方仅 Windows/Linux） |
| icqq-onebot + NT 协议 | 需签名服务，可用的 qsign 项目全部归档或需 Linux |
| icqq-onebot + `wt` 旧协议 | **实测被腾讯封堵**：滑块通过后返回 `code=45 禁止登录` |
| 官方 Bot API | 需企业/个体户资质，私聊能力受限 |

最终方案：**不碰协议层**，用腾讯官方客户端 + 系统 Accessibility API。

---

## 二、环境与前置条件（缺一不可）

| 项目 | 值 / 要求 |
|---|---|
| 机器 | MacBook Air M1（Apple Silicon），macOS 15.2 |
| QQ | 官方客户端 `/Applications/QQ.app`，**用的小号登录**（不是主号） |
| QQ 客户端架构 | Chromium 系（`QQNT.framework` + 多 Helper 进程） |
| 目标会话 | 大号已把机器人号备注为 **`DSH-BOT`**（写在本地 `.env` 的 `QQ_TARGET`） |
| DSH | Web 服务运行在 `http://127.0.0.1:3080`，profile 是 `web` |
| 模型 | `workbuddy / deepseek-v4.1-flash`，`reasoningEffort=low`（**用户明确要求不换模型**） |
| Python | 系统自带 `/usr/bin/python3` 3.9.6，**无第三方依赖**（只用标准库 + ctypes） |

### 必须授权的系统权限

1. **辅助功能**（读 AX 树、合成键盘事件）
   系统设置 → 隐私与安全性 → 辅助功能 → 添加 DSH 宿主进程
   （实测是 `/Users/gusheng/.nvm/versions/node/v24.21.0/bin/node`，PID 会变但可执行文件路径固定）
2. **屏幕录制**（图片理解截图）
   `screencapture` 已实测可用，无需额外授权动作

> ⚠️ 本 shell 环境运行在 **Rosetta（x86_64）** 下，`uname -m` 返回 `x86_64` 是假象，
> 硬件是 arm64。测试过：arm64 原生模式下所有 AX 结论一致，架构不是限制因素。

---

## 三、Bug 状态（**改动前先看这里**）

> 更新时间：2026-10-19 10:20。**Bug 1 已修复**，修复见下。

### Bug 1：同一条消息被重复回复 —— ✅ 已修复

**原现象**（2026-10-19 10:11 日志）：

```
10:11:33 检测到新消息：'第四遍了，你真晕过去了？'   ← 这其实是我自己上一轮的回复
10:11:33 处理新消息：'饿晕了已经'                  ← 却把旧的用户消息又处理了一遍
10:11:42 已发送：'你这句话我听了七遍了'
10:11:49 检测到新消息：'你这句话我听了七遍了'       ← 又把自己的回复当新消息
10:11:49 处理新消息：'饿晕了已经'                  ← 再次处理同一条旧消息
```

**根因**：原代码 `if deep: candidate = deep` —— 无论 `record_changed` 是否为 True 都用 `deep`。
QQ 渲染滞后时 `deep` 仍是**上一条用户消息**，于是「只有摘要变成自己的回复」那一轮，
又把旧消息处理了一遍。

**已应用的修复**（`bridge.py` 约 1384~1413 行）：

```python
if record_changed:
    candidate = deep                    # 真有新的对方消息
else:
    candidate = info["preview"].strip() # 只是摘要变了（通常是自己发的）
    if not candidate or _is_own(candidate):
        continue                        # 跳过

if _is_own(candidate):                  # 兜底防渲染方向异常
    continue
```

配套改动：发送成功后**只有 `record_changed` 为真才更新 `last_incoming`**
（否则 deep 是旧值，更新它会让真正的新消息在下一轮被跳过）。

**验证**：用 5 个场景做了逻辑模拟，全部通过：

| 场景 | 期望 | 结果 |
|---|---|---|
| 只有摘要变成自己的回复 | skip | ✅ |
| 用户发来新消息（记录变了） | process | ✅ |
| 用户新消息但摘要滞后 | process | ✅ |
| 渲染异常：对方消息其实自己发的 | skip | ✅ |
| 无任何变化 | none | ✅ |

实机启动后 30 秒内零误触发，状态稳定。

**如果再次出现重复回复**，按顺序排查：
1. 看日志是否有 `仅摘要变化（自己发的）` —— 有的话跳过是正确行为
2. 看 `state.json` 的 `last_incoming` 是否与 QQ 实际情况一致
3. 看 `own_sent` 里是否包含被重复处理的那条（`_is_own` 依赖它）

---

### Bug 2：进程管理混乱（运维级，不是代码缺陷）

会话后期反复出现：

```
❌ 另一个桥接实例已在运行（PID xxxxx），本次退出
桥接已退出 / 桥接已退出 / 桥接已退出   ← 多个实例互相干扰
```

原因：调试期间反复启停，`setsid` 不存在、`nohup &` 在工具 shell 里行为不稳定，
导致僵尸进程 + 残留 `data/bridge.lock`。

**清理的正确姿势**：

```bash
cd "/Users/gusheng/Documents/deepseek-harness/默认工作区/qq-dsh-bridge"
# 1. 按 PID 硬杀（pkill 在此环境下有时匹配不到）
ps aux | grep "[b]ridge.py" | awk '{print $2}' | while read p; do kill -9 $p; done
# 2. 删锁和 pid
rm -f data/bridge.lock data/bridge.pid
# 3. 确认干净
ps aux | grep "[b]ridge.py" | wc -l   # 应为 0
```

**启动的稳定方式**（这个环境里唯一可靠的）：用工具的 `run_in_background` 跑
`cd <目录> && python3 bridge.py`，而**不要**用 `nohup ... &` 或 `setsid`
（`setsid` 在这台机器上根本不存在）。

---

### Bug 3：`_SkipSend` 会让消息卡住（已部分修复，需验证）

「你说在忙就跳过发送」的路径会 `raise _SkipSend()`。
早期版本在跳过时已经把 `last_incoming` 更新了，导致这条消息**永远丢失**。
现在改成「只有真正发送成功才更新 `last_incoming`」，但**尚未经过完整回归验证**。

---

## 四、当前未提交的改动（重要！）

`git status`：

```
 M .env.example           # 新增 20 行配置项说明
 M bridge.py              # 改了约 250 行（见下）
 M qq-workspace/AGENTS.md # 新增日记/印象/图库/图片规则
 M reply_style.py         # 新增英文思考泄漏过滤
?? qq_capture.py          # 新增：窗口截图工具
?? vision.py              # 新增：图片理解
```

**上个会话结束时没有 commit，也没有 push。** 用户明确要求：

> 「不要自作主张直接推，我说推你再推」

未提交改动包含这些新功能：

1. **英文思考泄漏过滤**（`reply_style.py: strip_reasoning()`）
   —— 模型偶尔把 `Hmm, but instruction says...` 这类思考混进正文发出
2. **图片理解**（`vision.py` + `qq_capture.py`）
   —— 截图窗口 → 存文件 → 让模型用 `read` 工具自己看
3. **秒回节奏**（`QQ_POLL_MAX` 30→10，发送前延迟 0~0.3s，条间 0.5~1.5s）
4. **每分钟限流**（`MAX_SENDS_PER_MINUTE=20`，实际刹车在 18 条）
5. **不抢控制权**（`wait_until_user_idle()`：你在打字/动鼠标就不发）
6. **竞态重试**（`_send_one()` 带 3 次重试 + 每步确认 QQ 仍在前台）
7. **待发队列**（发送失败 `stash_pending()`，下一轮优先补发）
8. **检测修复**（每轮都读聊天记录 `read_chat_last_incoming()`，不依赖会话摘要刷新）

---

## 五、文件清单与职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `bridge.py` | 1674 | 主程序：轮询、检测、调 DSH、发送、状态机 |
| `dsh_api.py` | 155 | DSH Web API 客户端（离线铸造 Cookie + 斜杠 RPC） |
| `context_builder.py` | 94 | 输入层：拼情景头 + 最近对话 |
| `reply_style.py` | 266 | 输出层：markdown 清洗、客套话剥离、思考泄漏过滤、拆条 |
| `companion.py` | 303 | 进阶 5：主动找人调度、印象、图库、时间感 |
| `diary.py` | 180 | 进阶 1：日记读写与解析 |
| `vision.py` | 171 | 图片理解：截图调度、占位检测 |
| `qq_capture.py` | 101 | 独立的截图探测脚本（可单独跑） |
| `启动桥接.command` | — | 双击启动 |
| `停止桥接.command` | — | 双击停止 |
| `README.md` | — | 对外文档（含技术选型依据） |
| `使用说明.md` | — | 操作手册 |
| `.env.example` | — | 配置模板（**会提交**） |
| `.env` | — | 本地配置（**已 gitignore，含 `QQ_TARGET=DSH-BOT`**） |
| `qq-workspace/AGENTS.md` | — | **人格设定**（会提交，这是关键） |
| `qq-workspace/日记/diary.jsonl` | — | 运行数据（gitignore） |
| `qq-workspace/印象/*.json` | — | 运行数据（gitignore） |
| `qq-workspace/截图/*.png` | — | 运行数据（gitignore） |
| `data/bridge.log` | — | 运行日志（gitignore） |
| `data/state.json` | — | 状态机（gitignore） |
| `data/dsh-session.txt` | — | 绑定的 DSH 会话 ID（gitignore） |

---

## 六、运行方式

### 启动（推荐用后台任务）

```bash
cd "/Users/gusheng/Documents/deepseek-harness/默认工作区/qq-dsh-bridge"
python3 bridge.py
```

正常启动日志：

```
QQ ↔ DSH 桥接已启动
  目标会话 : DSH-BOT
  DSH 接入 : http://127.0.0.1:3080 / session-xxxxxxxx
  轮询     : 5.0~10.0s 随机
  发送间隔 : 3.0~5.0s，每分钟≤18、每小时≤60
图片理解已开启（截图保留 2 天）
日记已开启（今日 N/3）
主动找已开启（每 45~110 分钟检查...）
```

### 停止

```bash
双击 停止桥接.command
# 或
ps aux | grep "[b]ridge.py" | awk '{print $2}' | while read p; do kill -9 $p; done
rm -f data/bridge.lock data/bridge.pid
```

### 单独调试某个能力

```bash
# 探测 QQ 窗口结构
python3 -c "import sys; sys.path.insert(0,'.'); import bridge as B; qq=B.QQ(); qq.refresh(); print(qq.read_chat_structured()[-3:])"

# 截图是否可用
python3 qq_capture.py

# 检查系统空闲时间（不抢控制权依赖它）
python3 -c "import sys; sys.path.insert(0,'.'); import bridge as B; print(B.system_idle_seconds(), B.user_is_typing())"

# 检查 DSH API 连通
python3 -c "import sys; sys.path.insert(0,'.'); import dsh_api; print(dsh_api.DshApi().ping())"
```

---

## 七、关键技术细节（改动前必读）

### 7.1 读取：Accessibility API

QQ 是 Chromium 系，AX 树完整。实测可靠的定位方式：

| 目标 | 定位 |
|---|---|
| 会话列表项 | class 含 `recent-contact-item` |
| 昵称 | `item__info` 下第一个 `AXStaticText` 的 `AXValue` |
| 消息预览 | `item__info` → `summary-main` → `AXStaticText` |
| 时间 | `item__info` → `secondary-info` → `AXStaticText` |
| 消息列表 | class 含 `chat-msg-area` → `ml-item`（每条一个） |
| **消息方向** | class 含 `message-container--self` = 自己发的 |
| 消息文本 | `ml-item` 下的 `AXStaticText`（`AXValue`） |
| 输入框 | `msg-input` 容器内的 `AXTextArea`（CKEditor） |

**必须调用 `AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)`**
否则 Chromium 的按需 AX 树是空的。

**读取全程不需要 QQ 在前台**——已验证在 Safari 前台时能正常读。

### 7.2 发送：为什么必须切屏

**核心限制**：macOS 的 WindowServer 只把键盘事件路由给**前台应用的 key window**。
后台窗口没有 key window，所有投递方式都会在系统层被过滤。

已穷尽验证、**全部无效**的方案（不要再试）：

| 方案 | 结果 |
|---|---|
| AX 写 `AXValue` | 值被 CKEditor 重置 |
| AX 写 `AXSelectedText`（业界主力） | 返回成功但无实际输入 |
| `AXFocused = True` 后台设置 | 立即被系统重置 |
| `CGEventPostToPid`（x86_64 与 arm64 原生） | 事件被丢弃 |
| `CGEventPost` × 3 个 tap 层 | 只投给前台 |
| 私有 API `AXUIElementPostKeyboardEvent` | 返回 0 但无输入 |
| 像素坐标后台点击 + 输入 | 点击不激活编辑态 |
| Electron `SIGUSR1` 开 inspector | QQ 用自研 `QQNT.framework`，非标准 Electron |
| CDP 注入 JS | QQ 本地端口是私有 JWT 协议，不是 CDP |
| LLDB / 进程注入 | SIP 拦截：`Not allowed to attach to process` |

**业界同类项目对照**（都得出同样结论）：
- [trycua/cua](https://github.com/trycua/cua)（⭐30k）：对 Electron/Chromium 返回 `background_unavailable`，建议 `delivery_mode:"foreground"`
- [XXXXXQ-0206/qq-desktop-messaging](https://github.com/XXXXXQ-0206/qq-desktop-messaging)：原文「Hidden/background AX is an optimization, not a requirement」

**所以发送流程只能是**：
```
记录当前前台应用 → activate QQ → 写入 + 回车 → 还原原应用（约 1~2 秒）
```

### 7.3 与 DSH 的对接

不另起 `headless` 进程，而是直连正在运行的 Web 服务：

1. 读 `~/.dsh/.credentials.yaml` 的 `client-connection/browser-session` 密钥（32 字节）
2. 离线铸造 Cookie：`dsh-auth-<sha256(authority)>` = `v1.<b64payload>.<hmac>`
3. 调 `/api/session/prompt`（信封 `{type:"client-request", rpcId, method, payload:{args:{request:{...}}}}`）
4. 轮询 `/api/session/projections` 的 `turnOutline[].response` 拿回复

**各端点的 args 包装字段名不一样**（踩过的坑）：

| 端点 | 包装字段 |
|---|---|
| `session/list` | `_request` |
| `session/create` / `prompt` / `selectModel` / `rename` / `projections` | `request` |
| `session/modelCatalog` | 无包装（直接 `{}`） |

`session/prompt` 的 request 结构：
```python
{"sessionId": sid, "requestId": uuid, "mode": "queue",
 "content": [{"type": "text", "text": "..."}]}
```

`session/selectModel` **必须同时给 provider 和 model**，否则会被覆盖成默认值。

### 7.4 人格层怎么生效

`qq-workspace/AGENTS.md` 由 `@deepseek-ai/dsh-agent-instructions` 按 **cwd 链**注入。
规则：
- 项目根由 `.git` 标记（`qq-dsh-bridge/.git`）
- 从项目根到会话 cwd（`qq-workspace/`）之间的所有 `AGENTS.md` 都会被加载
- **在会话首次请求时加载**，改完必须让桥接新建会话（删 `data/dsh-session.txt` 重启）

实测效果：「你是谁啊」→「我啊，就一网友，你谁啊」；「你是不是AI」→「你才AI」。

---

## 八、配置参数（当前生效值）

全部可通过 `.env` 或环境变量覆盖（环境变量优先）。

| 参数 | 当前值 | 说明 |
|---|---|---|
| `QQ_TARGET` | `DSH-BOT` | 目标会话备注名（**必填**） |
| `QQ_DSH_URL` | `http://127.0.0.1:3080` | DSH 服务地址 |
| `QQ_POLL_MIN` / `MAX` | 5 / 10 | 轮询间隔（秒） |
| `QQ_SEND_GAP_MIN` / `MAX` | 3 / 5 | 两条回复间隔 |
| `QQ_MAX_SENDS_PER_MINUTE` | 20 | **QQ 硬限制** |
| `QQ_SEND_LIMIT_MARGIN` | 18 | 自己先刹车的位置 |
| `QQ_MAX_SENDS_PER_HOUR` | 60 | 每小时上限 |
| `QQ_SPLIT_MAX_CHARS` | 60 | 拆条字数 |
| `QQ_SHORT_REPLY_PROB` | 0.10 | 只回第一条的概率 |
| `QQ_REASONING_EFFORT` | `low` | 推理档位（`off` 不被 workbuddy 支持） |
| `QQ_MAX_SESSION_TURNS` | 40 | 轮数超限换新会话 |
| `QQ_CONTEXT_HISTORY` | 6 | 投递时带几条历史 |
| `QQ_DIARY_ENABLED` | 1 | 日记开关 |
| `QQ_DIARY_MAX_PER_DAY` | 3 | 一天最多写几条 |
| `QQ_PROACTIVE_ENABLED` | 1 | 主动找人开关 |
| `QQ_PROACTIVE_CHECK_MIN` / `MAX` | 45 / 110 | 检查间隔（分钟，随机） |
| `QQ_PROACTIVE_IDLE_MIN` | 15 | 安静多久才考虑开口（分钟） |
| `QQ_PROACTIVE_PROB` | 0.35 | 每次真发的概率 |
| `QQ_VISION_ENABLED` | 1 | 图片理解开关 |
| `QQ_SCREENSHOT_KEEP_DAYS` | 2 | 截图保留天数 |
| `QQ_TYPING_IDLE_SEC` | 3 | 空闲低于几秒算「在用电脑」 |
| `QQ_TYPE_GUARD_MAX_WAIT` | 180 | 等用户空闲的上限（秒） |
| `QQ_TYPING_DELAY_MIN` / `MAX` | 0 / 0.3 | 发送前延迟（秒） |
| `QQ_SLOW_REPLY_PROB` | 0.05 | 故意慢一拍的概率 |
| `QQ_SLOW_REPLY_MAX` | 6 | 慢一拍的上限（秒） |
| `QQ_DEEP_CHECK_INTERVAL` | 8 | 兜底读聊天记录的间隔（秒） |
| `QQ_DEBUG` | 0 | 设为 1 输出 DEBUG 日志 |

---

## 九、性能基线（实测数据）

| 环节 | 耗时 |
|---|---|
| 模型思考（纯文本） | **2.9 秒**（三轮实测 3.0/2.9/2.9） |
| 模型思考（带图片） | 8~10 秒（要调 `read` 工具看截图） |
| 轮询检测 | 5~10 秒 |
| 发送动作 | 1~2 秒 |
| **纯文本总延迟** | **典型 10~15 秒** |
| 图片消息总延迟 | 15~25 秒 |

用户目标是「15 秒以内」，纯文本已达标。

---

## 十、历史踩坑速查（省时间用）

| 症状 | 真因 | 处理 |
|---|---|---|
| 消息很久不回复 | **会话列表摘要不刷新**，`sig` 永远不变 | 已修：每轮读 `read_chat_last_incoming()` |
| 同一条消息回好几遍 | `deep` 在渲染滞后时还是旧值 | **见第三节 Bug 1，待修** |
| 回复慢 90 秒 | 旧 `user_is_typing()` 只要焦点在输入框就判「在忙」 | 已修：改用 IOKit `HIDIdleTime` |
| 发送失败丢消息 | 用户点屏幕抢走焦点 | 已修：重试 + `_qq_is_frontmost()` + 待发队列 |
| 报「另一个实例已在运行」 | 残留锁文件 + 僵尸进程 | `kill -9` 所有进程 + 删 `data/bridge.lock` |
| `setsid: command not found` | 这台 Mac 没有 setsid | 用工具的 `run_in_background` |
| 启动日志显示但没反应 | QQ 未登录 / 备注名不匹配 | 检查 QQ 是否在线、`QQ_TARGET` 拼写 |
| AX 树是空的 | 没开 `AXManualAccessibility` | `qq.refresh()` 里已处理 |
| 人格设定不生效 | `AGENTS.md` 只在会话**首次请求**时加载 | 删 `data/dsh-session.txt` 后重启 |
| 模型 selectModel 失败 | 只传了 `reasoningEffort` 没传 provider/model | 用 `resolve_model()` 先取现有值 |

---

## 十一、用户的明确偏好（务必遵守）

1. **不要自作主张 push**：「我说推你再推」
2. **不换模型**：就用 `workbuddy / deepseek-v4.1-flash`
3. **要快**：回复控制在 15 秒内，砍掉多余的「打字模拟」
4. **不抢控制权**：用户打字/动鼠标时不发，等停下再发
5. **要能看懂图片/表情包**：不能只会装傻
6. **越像真人越好**：不要助手腔、不要分点、不要总结
7. **限制发送频率**：每分钟不超过 20 条（QQ 服务端硬限制，超了会被整段限速）

---

## 十二、下一步待办（按优先级）

1. ~~修 Bug 1（重复回复）~~ —— ✅ **已完成**，见第三节
2. **实机回归**：让用户发一条消息，确认只回复一次、且延迟在 15 秒内
3. **验证 Bug 3**（`_SkipSend` 后消息是否会丢）—— 逻辑上已随 Bug 1 一并修好，需实测
4. **跑一轮完整回归**：发文字、发图片、打字时发、连续发多条
5. **提交本轮全部改动**（8 项新功能 + Bug 1 修复），等用户说「推」再 push
6. **补文档**：README 里补「秒回模式」「不抢控制权」「图片理解」三节
7. 可选：`app not frontmost` 时的截图兜底（现在截图要求窗口可见）

---

## 十三、给新会话的三句话

1. **别再去试协议端和「无焦点发送」**——已穷尽验证，macOS 上不可能，理由写在 7.2。
2. **改动前先看第七节**，尤其 AX 定位方式和 DSH API 的 args 包装字段名的坑。
3. **Bug 1（重复回复）已修**，当前最该做的是实机回归验证，而不是继续加功能。
