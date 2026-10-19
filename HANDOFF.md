# 会话交接文档 · qq-dsh-bridge

> 最后更新：2026-10-19 11:25。新会话读完这份就能接着干，不必从头摸索。
> 项目地址：https://github.com/Gusheng366/qq-dsh-bridge-MacOS （公开，MIT）

---

## 零、现在是什么状态

| 项目 | 状态 |
|---|---|
| 桥接进程 | **运行中**（`python3 bridge.py`，调试模式 `QQ_DEBUG=1`） |
| 主模型 | `workbuddy/deepseek-v4.1-flash` **额度耗尽**，自动降级到 `space-bunny` |
| 已在用备用 | `workbuddy/space-bunny`（约 4~6 秒回复） |
| 回复延迟 | 典型 9~19 秒 |
| git | 本地 `main`，比远程多若干提交 |

**接下来最该做的**：等主模型充值后确认能自动切回（后台每 90 秒复检一次）。

---

## 一、项目是什么

把 **macOS 上的官方 QQ 桌面客户端**接入 DeepSeek Harness（DSH），实现「用另一个 QQ 号跟 AI 聊天」。

```
QQ 官方客户端(小号) ⇄ Accessibility API ⇄ 桥接 ⇄ DSH Web API ⇄ Agent 会话
```

### 为什么不用协议端（已穷尽验证，不要再试）

| 方案 | 结论 |
|---|---|
| SnowLuma / NapCat | macOS 无移植（官方仅 Windows/Linux） |
| icqq-onebot + NT 协议 | 需签名服务，可用的 qsign 项目全部归档或需 Linux |
| icqq-onebot + `wt` 旧协议 | **实测被腾讯封堵**：滑块通过后返回 `code=45 禁止登录` |
| 官方 Bot API | 需企业/个体户资质，私聊能力受限 |

**最终方案：不碰协议层**，用腾讯官方客户端 + 系统 Accessibility API，因此不存在协议特征封号问题。

---

## 二、环境与前置条件

| 项目 | 值 |
|---|---|
| 机器 | MacBook Air M1（Apple Silicon），macOS 15.2 |
| QQ | 官方客户端 `/Applications/QQ.app`，**小号登录** |
| QQ 架构 | Chromium 系（`QQNT.framework` + 多 Helper 进程） |
| 目标会话 | 大号把机器人号备注为 `DSH-BOT`（写在本地 `.env` 的 `QQ_TARGET`） |
| DSH | Web 服务 `http://127.0.0.1:3080`，profile `web` |
| 模型 | `workbuddy / deepseek-v4.1-flash` @ `low`（用户明确不换主模型） |
| Python | 系统 `/usr/bin/python3` 3.9.6，**零第三方依赖**（标准库 + ctypes） |

### 必须授权的系统权限

1. **辅助功能**（读 AX 树、合成键盘事件）
   → 系统设置 → 隐私与安全性 → 辅助功能 → 添加 DSH 宿主进程
   （实测是 `~/.nvm/versions/node/v24.21.0/bin/node`）
2. **屏幕录制**（图片理解的截图）
   → `screencapture` 已实测可用

> ⚠️ 本 shell 在 **Rosetta** 下，`uname -m` 返回 `x86_64` 是假象，硬件是 arm64。
> 已实测 arm64 原生模式结论一致，架构不是限制因素。

---

## 三、文件清单

| 文件 | 职责 |
|---|---|
| `bridge.py` | 主程序：轮询、检测、选模型、调 DSH、发送、状态机 |
| `dsh_api.py` | DSH Web API 客户端（离线铸造 Cookie + 斜杠 RPC） |
| `context_builder.py` | 输入层：情景头 + 最近对话 |
| `reply_style.py` | 输出层：markdown 清洗、客套话剥离、思考泄漏过滤、拆条 |
| `companion.py` | 主动找人调度、印象、图库、时间感 |
| `diary.py` | 日记读写与解析 |
| `vision.py` | 图片理解：截图调度、占位检测 |
| `schedule.py` | 课表运行时：读 schedule.json，生成注入摘要 |
| `schedule_import.py` | 课表导入器：ICS 展开成扁平课表（换课表时跑） |
| `qq_capture.py` | 独立截图探测脚本（可单独跑） |
| `启动桥接.command` / `停止桥接.command` | 双击启停 |
| `README.md` / `使用说明.md` | 对外文档 / 操作手册 |
| `HANDOFF.md` | 本文件 |
| `.env.example` | 配置模板（**会提交**） |
| `.env` | 本地配置（gitignore，含 `QQ_TARGET=DSH-BOT`） |
| `qq-workspace/AGENTS.md` | **人格设定**（会提交，关键） |
| `qq-workspace/课程表/*` | 课表数据（gitignore，由导入器生成） |
| `qq-workspace/{日记,印象,图库,截图}/*` | 运行数据（gitignore） |
| `data/bridge.log` `state.json` `dsh-session.txt` | 运行状态（gitignore） |

---

## 四、运行方式

### 启动

```bash
cd "<项目目录>"
python3 bridge.py            # 正常
QQ_DEBUG=1 python3 bridge.py # 带轮询心跳/调试日志
```

**在这个环境里唯一稳定的后台启动方式**：用工具的 `run_in_background`。
`setsid` 不存在；`nohup ... &` 行为不稳定。

正常启动日志：

```
模块: workbuddy/deepseek-v4.1-flash → workbuddy/space-bunny → workbuddy/glm-5.3-flash
  探测超时 20s，成功缓存 600s，失败冷却 600s
课程表已加载（89 节课）
图片理解已开启（截图保留 2 天）
日记已开启（今日 N/3）
主动找已开启（距上次消息 10~40 分钟后主动发一条；概率 1.0）
```

### 停止

```bash
pgrep -f "python3 bridge.py" | while read p; do kill -9 $p; done
rm -f data/bridge.lock data/bridge.pid
```

> `pkill -f` 在这个环境里有时匹配不到，用上面的写法最可靠。

### 单独调试

```bash
# QQ 窗口结构 / 最近消息
python3 -c "import sys;sys.path.insert(0,'.');import bridge as B;q=B.QQ();q.refresh();print(q.read_chat_structured()[-3:])"
# 截图是否可用
python3 qq_capture.py
# 鼠标/键鼠空闲
python3 -c "import sys;sys.path.insert(0,'.');import bridge as B;print(B.mouse_idle_seconds(),B.system_idle_seconds())"
# DSH API 连通
python3 -c "import sys;sys.path.insert(0,'.');import dsh_api;print(dsh_api.DshApi().ping())"
```

---

## 五、关键技术细节（改动前必读）

### 5.1 读取：Accessibility API

QQ 是 Chromium 系，AX 树完整。实测可靠的定位：

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

**必须 `AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)`**，
否则 Chromium 的按需 AX 树是空的。读取全程不需要 QQ 在前台。

### 5.2 发送：为什么必须切屏

macOS 的 WindowServer 只把键盘事件路由给**前台应用的 key window**。
后台窗口没有 key window，所有投递方式都会在系统层被过滤。

已穷尽验证、**全部无效**（不要再试）：

| 方案 | 结果 |
|---|---|
| AX 写 `AXValue` | 被 CKEditor 重置 |
| AX 写 `AXSelectedText`（业界主力） | 返回成功但无输入 |
| `AXFocused = True` 后台设置 | 立即被系统重置 |
| `CGEventPostToPid`（x86_64 与 arm64） | 事件被丢弃 |
| `CGEventPost` × 3 个 tap 层 | 只投给前台 |
| 私有 API `AXUIElementPostKeyboardEvent` | 返回 0 但无输入 |
| 像素坐标后台点击 + 输入 | 点击不激活编辑态 |
| Electron `SIGUSR1` 开 inspector | QQ 用自研 `QQNT.framework`，非标准 Electron |
| CDP 注入 JS | QQ 本地端口是私有 JWT 协议，不是 CDP |
| LLDB / 进程注入 | SIP 拦截：`Not allowed to attach to process` |

**业界对照**：[trycua/cua](https://github.com/trycua/cua)（⭐30k）对 Electron/Chromium 返回
`background_unavailable`；[qq-desktop-messaging](https://github.com/XXXXXQ-0206/qq-desktop-messaging)
原文「Hidden/background AX is an optimization, not a requirement」。

发送流程只能是：记录前台 → activate QQ → 写入+回车 → 还原（1~2 秒）。

**竞态保护**：`_send_one()` 每次尝试前/回车前都确认 QQ 仍在前台，
被用户点走焦点就重试（最多 3 次）；仍失败进待发队列。

### 5.3 与 DSH 的对接

读 `~/.dsh/.credentials.yaml` 的 `client-connection/browser-session` 密钥，
HMAC 离线铸造 Cookie，然后调斜杠 RPC。

**各端点的 args 包装字段名不一样**（踩过的坑）：

| 端点 | 包装字段 |
|---|---|
| `session/list` | `_request` |
| `session/create` / `prompt` / `selectModel` / `rename` / `projections` | `request` |
| `session/modelCatalog` | 无包装（直接 `{}`） |

`session/prompt` 的 request：

```python
{"sessionId": sid, "requestId": uuid, "mode": "queue",
 "content": [{"type": "text", "text": "..."}]}
```

`session/selectModel` **必须同时给 provider 和 model**，否则会被覆盖成默认值。

回复读取：轮询 `session/projections` 的 `turnOutline[].response`。

### 5.4 人格层怎么生效

`qq-workspace/AGENTS.md` 由 `@deepseek-ai/dsh-agent-instructions` 按 **cwd 链**注入：

- 项目根由 `.git` 标记（`qq-dsh-bridge/.git`）
- 从项目根到会话 cwd（`qq-workspace/`）之间的所有 `AGENTS.md` 都会加载
- **在会话首次请求时加载**；改完要靠新建会话或文件操作触发重载

实测：「你是谁啊」→「我啊，就一网友，你谁啊」；「你是不是AI」→「你才AI」。

### 5.5 模型链与故障切换

```ini
QQ_MODEL_CHAIN=workbuddy/deepseek-v4.1-flash:low,workbuddy/space-bunny:low,workbuddy/glm-5.3-flash:low
```

1. **健康探测**：独立探测会话（cwd `data/probe`，故意避开人格 AGENTS.md），
   发一句「在吗 <随机>」，20 秒内出字算可用。
2. **快路径**：先只挑缓存里确认可用的模型，**零探测延迟**（实测 25s → 0.00s）。
3. **后台复检**：线程每 90 秒检查一次主模型（仅当它之前失败且已过冷却），
   恢复后自动切回，不阻塞回复。
4. **失败兜底**：真实投递后回复为空 → `mark_model_bad()`，下一条换下一个。
5. **全挂**：不投递（避免空回合污染历史），睡 30 秒重试，消息不丢。

**模型聊天适配实测**（同一句「好饿啊 下午还有课不想上了」）：

| 模型 | 延迟 | 回复 | 价格 |
|---|---|---|---|
| `space-bunny` | **3.1s** | 那你就翘啊，反正饿着也听不进去 `DIARY: …` | x0.08 |
| `glm-5.3-flash` | 6.1s | 饿就对了 不想上就摸鱼呗 反正也就一下午 | x0.06 |
| `hy3-x` | 7.6s | 饿就去吃呗 逃就逃了呗 | x0.05 |
| `step-5-preview` | 12.1s | 翘了呗 反正去了也不听 | 免费 |
| `hy3` | 26.6s | 青蛙还在哭呢 | 免费 |

选 `space-bunny` + `glm-5.3-flash` 做备用：最快、便宜，且都认 `AGENTS.md` 的
`DIARY:` / `IMPRESSION:` 标记（space-bunny 首次测试就自动写了日记）。

### 5.6 课表

`schedule_import.py` 把 ICS（RRULE 周重复）展开成扁平课表，只保留今天及以后：

```bash
python3 schedule_import.py <课表.ics>     # 输出到 qq-workspace/课程表/
```

产出：`schedule.json`（桥接读）、`schedule.md`（人看）、`未来课程.ics`（可导入日历）。

`schedule.py` 每轮注入约 300 字摘要，**必须包含三件事**（都是踩坑换来的）：

1. **下课时间**——只给开始时间时，模型会把后面那节的下课时间当成现在
2. **★正在上标记**——哪节正在上、还有几分钟下课
3. **归属**——「【他的课表（不是你的，你自己没有课）】」，
   否则模型会代入成「我这节课…」

摘要长这样：

```
【他的课表（不是你的，你自己没有课）】
现在 11:00
他今天（10月19日 周一）3节：
  08:00-09:40 园区网架构与技术（明辨楼406） —— 已下课
  10:00-11:40 广域承载网架构与技术（明辨楼406） —— ★正在上，11:40 下课（还有 39 分钟）
  16:30-18:10 Python应用开发技术（明辨楼408） —— 还没开始
他下一节：今天 16:30-18:10 Python应用开发技术
```

### 5.7 等待策略（不抢控制权）

**只看鼠标**，键盘在敲不影响：

```python
mouse_idle_seconds()  # 取 8 类鼠标事件空闲时间的最小值
                      # 左/右键按下抬起、移动、拖动、滚轮
```

- `QQ_IDLE_SOURCE=mouse`（默认）/ `any`（键鼠都算）
- 鼠标静止 ≥ `QQ_TYPING_IDLE_SEC`（3 秒）→ 立即发
- 最长等 `QQ_TYPE_GUARD_MAX_WAIT`（20 秒），超时跳过本次回复

### 5.8 主动发言

**距上一次消息 10~40 分钟后主动找一次**：

- 每条消息往来都 `note_activity()` 重新计时 → 间隔稳定落在 10~40 分钟
- 到点即发（概率 1.0，否则可能拖到 80 分钟）
- 正在聊（安静不足 10 分钟）不打断
- 模型可用 `SILENT` 表示这次不想说

---

## 六、配置参数（当前生效值）

| 参数 | 当前值 | 说明 |
|---|---|---|
| `QQ_TARGET` | `DSH-BOT` | 目标会话备注（**必填**） |
| `QQ_DSH_URL` | `http://127.0.0.1:3080` | DSH 地址 |
| `QQ_POLL_MIN` / `MAX` | 5 / 10 | 轮询间隔（秒） |
| `QQ_SEND_GAP_MIN` / `MAX` | 3 / 5 | 两条回复间隔 |
| `QQ_MAX_SENDS_PER_MINUTE` | 20 | **QQ 硬限制** |
| `QQ_SEND_LIMIT_MARGIN` | 18 | 自己先刹车的位置 |
| `QQ_MAX_SENDS_PER_HOUR` | 60 | 每小时上限 |
| `QQ_SPLIT_MAX_CHARS` | 60 | 拆条字数 |
| `QQ_SHORT_REPLY_PROB` | 0.10 | 只回第一条的概率 |
| `QQ_REASONING_EFFORT` | `low` | 推理档位 |
| `QQ_MAX_SESSION_TURNS` | 40 | 轮数超限换新会话并注入摘要 |
| `QQ_CONTEXT_HISTORY` | 6 | 附带历史条数 |
| `QQ_MODEL_CHAIN` | 主+2备 | 见 5.5 |
| `QQ_MODEL_PROBE_TIMEOUT` | 20 | 探测出字的最长等待（秒） |
| `QQ_MODEL_HEALTH_OK_TTL` | 600 | 成功缓存（秒） |
| `QQ_MODEL_HEALTH_FAIL_TTL` | 600 | 失败冷却（秒） |
| `QQ_MODEL_REFRESH_INTERVAL` | 90 | 后台复检主模型的间隔（秒） |
| `QQ_IDLE_SOURCE` | `mouse` | 等待策略：只看鼠标 |
| `QQ_TYPING_IDLE_SEC` | 3 | 鼠标静止几秒算停下 |
| `QQ_TYPE_GUARD_MAX_WAIT` | 20 | 等空闲上限（秒） |
| `QQ_TYPING_DELAY_MIN` / `MAX` | 0 / 0.3 | 发送前延迟（秒） |
| `QQ_SLOW_REPLY_PROB` | 0.05 | 故意慢一拍的概率 |
| `QQ_DEEP_CHECK_INTERVAL` | 8 | 兜底读聊天记录的间隔（秒） |
| `QQ_AX_POKE_AFTER` | 45 | 多久无变化就轻点会话项刷新（秒） |
| `QQ_DIARY_ENABLED` | 1 | 日记开关 |
| `QQ_DIARY_MAX_PER_DAY` | 3 | 一天最多写几条 |
| `QQ_PROACTIVE_ENABLED` | 1 | 主动发言开关 |
| `QQ_PROACTIVE_CHECK_MIN` / `MAX` | 10 / 40 | 距上次消息多久后主动发（分钟） |
| `QQ_PROACTIVE_IDLE_MIN` | 10 | 安静多久才可开口（分钟） |
| `QQ_PROACTIVE_PROB` | 1.0 | 到点后发送概率 |
| `QQ_VISION_ENABLED` | 1 | 图片理解开关 |
| `QQ_SCREENSHOT_KEEP_DAYS` | 2 | 截图保留天数 |
| `QQ_SCHEDULE_ENABLED` | 1 | 课表开关 |
| `QQ_DEBUG` | 0 | 1 = 输出 DEBUG 日志 |

---

## 七、性能基线（实测）

| 环节 | 耗时 |
|---|---|
| 轮询检测 | 5~10 秒 |
| 打开会话 + 读聊天记录 | < 1 秒 |
| 拼上下文 | < 0.1 秒 |
| 选模型（快路径） | **0.00 秒**（原来是 25 秒） |
| 模型思考（纯文本） | **约 4 秒** |
| 发送动作 | 4~5 秒（含 3~5 秒两条间隔） |
| **端到端** | **约 14~19 秒** |
| 图片消息 | 多 8~10 秒（模型要 `read` 截图） |

想更快：`QQ_POLL_MIN=3 QQ_POLL_MAX=5 QQ_SEND_GAP_MIN=1 QQ_SEND_GAP_MAX=2`
→ 可到 8~12 秒。

---

## 八、踩坑速查

| 症状 | 真因 | 处理 |
|---|---|---|
| 消息很久不回（**5 分钟**） | 桥接进程的 AX 视图过期，一直读旧摘要 | 已修：45 秒无变化轻点会话项刷新 |
| 同一条消息回好几遍 | 检测分支只看 `deep` 非空就处理，没区分信号 | 已修：按 `record_changed` 分情况 |
| 回复被拖到 73 秒 | 模型探测阻塞 25s + 等空闲 39s | 已修：快路径 + 上限 20s |
| AX 树整个读不出来 | **把 `AXManualAccessibility` 置 FALSE 再 TRUE** | ⚠️ **千万别这么做**，会彻底关掉 QQ 的无障碍树，只能回退操作才能恢复 |
| 回「我这节课…」 | 课表没写归属 | 已修：标题写明「他的课表，不是你的」 |
| 回成下午课的下课时间 | 摘要只给开始时间、没标正在上 | 已修：给区间 + ★正在上 |
| 报「另一个实例已在运行」 | 残留锁 + 僵尸进程 | `kill -9` 全部 + 删 `data/bridge.lock` |
| `setsid: command not found` | 这台 Mac 没有 setsid | 用工具的 `run_in_background` |
| 人格设定不生效 | `AGENTS.md` 只在会话首次请求时加载 | 删 `data/dsh-session.txt` 后重启 |
| `selectModel` 失败 | 只传了 `reasoningEffort` | 必须同时给 provider + model |
| **模型不回复、response 为空** | **额度耗尽**（`llmMs=0`、`decodeTokens=0`） | 见下方判断脚本 |

### 额度耗尽怎么判断

症状：`session/prompt` 返回 `ok: True`，但 `turnOutline[-1].response` 永远空，
`sessionStats.llmMs = 0`、`decodeTokens = 0`，`running: False`。

```bash
cd "<项目目录>"
python3 - <<'PY'
import sys, uuid, time; sys.path.insert(0,'.')
import bridge as B
api = B.DshApi(B.DSH_BASE_URL)
st, d = api.rpc('session/create', {'request': {'cwd': str(B.QQ_SESSION_CWD)}})
sid = d['result']['value']['sessionId']
api.rpc('session/selectModel', {'request': {
    'sessionId': sid, 'provider': 'workbuddy',
    'model': 'deepseek-v4.1-flash', 'reasoningEffort': 'low'}})
api.rpc('session/prompt', {'request': {
    "sessionId": sid, "requestId": str(uuid.uuid4()), "mode": "queue",
    "content": [{"type": "text", "text": "只回复两个字：测试"}]}})
for _ in range(30):
    time.sleep(2)
    st, d = api.rpc('session/projections', {'request': {'sessionId': sid}})
    o = d['result']['value']['values'].get('turnOutline', [])
    if o and o[-1].get('response'):
        print("✅ 额度正常:", o[-1]['response']); break
else:
    print("❌ 额度耗尽或无响应")
PY
```

---

## 九、用户的明确偏好（务必遵守）

1. **不要自作主张 push**：说了「推」才推
2. **不换主模型**：就用 `workbuddy / deepseek-v4.1-flash`
3. **要快**：回复控制在 15 秒内，砍掉多余打字模拟
4. **不抢控制权**：只看鼠标，鼠标不动就可以操作
5. **要能看懂图片/表情包**
6. **越像真人越好**：不要助手腔、不分点、不总结
7. **限制发送频率**：每分钟不超过 20 条
8. **额度用完先停**，不要自动烧别的额度
9. **课表是他的**，AI 自己没有课

---

## 十、下一步待办

1. **验证主模型恢复后能自动切回**（充值后看日志 `后台复检主模型 … 已恢复`）
2. **回归测试**：发文字 / 发图片 / 打字时发 / 连续多条 / 问课表 / 等主动发言
3. **补 README**：秒回模式、不抢控制权、图片理解、课程表、模型链五节
4. 可选：把轮询和发送间隔再调快（见第七节）
5. 可选：`app not frontmost` 时的截图兜底（现在截图要求窗口可见）

---

## 十一、给新会话的三句话

1. **别试协议端和「无焦点发送」**——已穷尽验证，macOS 上不可能，理由在 5.2。
2. **改动前先看第五节**，尤其 AX 定位表、DSH 的 args 包装字段名、
   以及「千万别把 `AXManualAccessibility` 置 FALSE」这个坑。
3. **先看日志再动手**：`data/bridge.log` 里已有轮询心跳和分段日志，
   能直接判断是「没检测到」「模型挂了」还是「在等你空闲」。
