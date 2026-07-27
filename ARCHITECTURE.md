# AiChat Android 架构与扩展指南

> 本文档记录 v4.2.6 时的实际架构，并给出后续接入 Proactive Agent 的扩展点。
> 修改 streaming/conversation 相关代码前先读这份。

---

## 一、当前架构（v4.2.6）

### 1.1 进程模型

App 跑在**两个 Linux 进程**里，共享同一个 APK、同一个 Room DB 文件、同一个 FileProvider：

| 进程 | 包名 | 角色 |
| --- | --- | --- |
| main | `com.example.aichat` | UI 进程，跑 Activity / Compose / ViewModel |
| :streaming | `com.example.aichat:streaming` | 前台服务进程，跑 SSE / WakeLock / 持久化 |

**为什么拆进程？**
国产 ROM（小米/华为/OPPO/vivo）在锁屏 / 切应用到后台一段时间后会激进地杀掉主进程。如果 SSE 跑在主进程，连接必死。**独立进程 + 前台通知 + WakeLock** 是目前能在国产 ROM 上稳定跑长连接的最稳组合。

**两进程怎么通信？**
不直接通信，靠 **Room 多实例失效**——两个进程各自打开同一个 DB 文件，A 进程写入后 B 进程的 Flow observer 自动收到失效通知并重新查询。这是 Android 上最稳的跨进程状态同步方式之一。

### 1.2 数据流：一次 AI 回复的全过程

```
用户点发送
   │
   ▼
[main] ChatViewModel.sendMessage(text)
   │   1. repository.appendMessage(userMessage)
   │   2. lastConversationTracker.save(convId)        ← v4.2.4，进程死掉后能恢复
   │   3. StreamingService.start(...)                 ← 跨进程 Intent
   │
   ▼
[:streaming] StreamingService.onStartCommand
   │   1. startForeground(NOTIF)                      ← 必须在 5s 内调用（Android 14+）
   │   2. wakeLockProvider.acquire(10min)             ← PARTIAL_WAKE_LOCK
   │   3. serviceScope.launch { useCase() }
   │
   ▼
[:streaming] StreamAiReplyUseCase
   │   1. markConversationStreamingInterrupted(conv)  ← 清掉上一轮残留的 STREAMING 行
   │   2. repository.appendMessage(aiPlaceholder)     ← status=STREAMING, content=""
   │   3. repository.getMessages(conv) → build history
   │   4. remoteDataSource.streamChat(history)        ← 返回 Flow<StreamEvent>
   │   5. stream.collect {
   │        ContentDelta   → contentBuilder.append
   │        ReasoningDelta → reasoningBuilder.append
   │        每 150ms: maybePersistStreaming(写回 DB)
   │      }
   │   6. finalize (NonCancellable):
   │      - finalContent 有内容 → status=COMPLETE
   │      - finishedNormally=false → status=INTERRUPTED
   │      - apiError 非 null → status=FAILED
   │      - **永远 UPDATE，永不 DELETE**（v4.2.5）
   │
   ▼
[main] Room observer (observeMessages)
   │   收到 DB 变更，刷新 uiState.messages
   │   LazyColumn 重组，渲染流式光标
   ▼
用户看到 AI 回复
```

### 1.3 锁屏场景的存活路径

```
T0      用户在 conv A 发消息
T0+50   :streaming 启动 → AI placeholder 入库（STREAMING）
T0+1s   SSE 流式 → content/reasoning 每 150ms 持久化
        messages.updatedAt 每 150ms 刷新                ← v4.2.6 新增字段
T_lock  用户锁屏
        ├ Android 杀主进程 → 用户解锁后 ChatViewModel 重启
        │   ├ restoreLastConversation 自动恢复 conv A（v4.2.4）
        │   └ WATCHDOG_DELAY_MS=3s 后跑 watchdog:
        │     "把 updatedAt < now-2min 的 STREAMING 行标 INTERRUPTED"
        │     ✗ 刚才 :streaming 还在写，updatedAt 是几百毫秒前 → 不动它
        │
        └ :streaming 在 wakeLock 保护下继续流式
            即便主进程被杀，DB 还在更新，解锁后 UI 自然恢复 ✓
```

**v4.2.6 之前的问题：** watchdog 是"无差别 sweep"，把所有 STREAMING 行都标 INTERRUPTED。锁屏回来刚好踩进这个窗口，活跃流被误杀。

**v4.2.6 的修复：** 加 `updatedAt` 字段，watchdog 用 2 分钟阈值，只动真正孤儿行。

### 1.4 SSE 重试逻辑（v4.2.2 引入）

`OpenAiCompatibleProvider.stream()` 在 `flow {}` 里跑一个 `while(true)` 重试循环：

```
state: hasEmittedAnyDelta = false
       attempt = 0

loop:
  responseBody ← apiService.streamChat()
      on IOException (建连失败):
          attempt++ ; 若 >3 次 → throw NetworkException
          delay([1s,2s,4s][attempt]) ; continue
      on HttpException:
          读 errorBody → throw ApiException

  streamClient.toEventFlow(responseBody).collect {
      emit(event)
      if delta: hasEmittedAnyDelta = true
  }
  正常结束 → return@flow

  catch CancellationException → re-throw (协作取消，不重试)
  catch IOException (中途断流):
      if hasEmittedAnyDelta: return@flow  ← 已吐过内容，静默结束当 COMPLETE
      else: 同建连失败路径，重试
```

**关键不变量：** 一旦 `hasEmittedAnyDelta = true`，不再重试——重试会让 LLM 重新生成、产出重复 token，比断流更糟。

### 1.5 文件结构（关键源码索引）

```
app/src/main/java/com/example/aichat/
├── AiChatApplication.kt                ← @HiltAndroidApp，启动时 seed ApiProfile
├── MainActivity.kt
├── data/
│   ├── local/
│   │   ├── AppDatabase.kt              ← Room DB（v10）
│   │   ├── Migrations.kt               ← MIGRATION_2_3 ... 9_10
│   │   ├── entity/                     ← MessageEntity / ConversationEntity / ...
│   │   ├── dao/                        ← MessageDao / ConversationDao / ...
│   │   └── LastConversationTracker.kt  ← v4.2.4 SharedPreferences
│   ├── remote/
│   │   ├── AiApiService.kt             ← Retrofit 接口
│   │   ├── AiStreamClient.kt           ← SSE 解析 → Flow<StreamEvent>
│   │   ├── ChatRemoteDataSource.kt     ← 桥接 domain Message ↔ LlmProvider
│   │   └── dto/                        ← OpenAI 兼容请求/响应
│   ├── provider/
│   │   ├── LlmProviderFactory.kt       ← OkHttp+Retrofit per-profile，含 SSE 重试
│   │   └── SupplierRegistry.kt         ← 内置供应商（OpenAI / Claude / glm / kimi / ...）
│   ├── repository/                     ← ChatRepositoryImpl 等
│   └── security/                       ← ApiKeyEncryptor
├── domain/
│   ├── model/                          ← Message / Conversation / Role / Status
│   ├── repository/                     ← ChatRepository（接口）
│   └── usecase/
│       └── StreamAiReplyUseCase.kt     ← :streaming 进程的主流程
├── service/
│   └── StreamingService.kt             ← 前台服务 + WakeLock + Job Map
├── di/                                 ← Hilt 模块
└── ui/
    ├── chat/
    │   ├── ChatScreen.kt               ← 主屏（LazyColumn + 输入栏 + 抽屉）
    │   ├── ChatViewModel.kt
    │   ├── MessageBubble.kt
    │   ├── ChatInputBar.kt
    │   ├── ReasoningSection.kt
    │   ├── HtmlCard.kt                 ← 流式中检测到完整 HTML 页时改用 WebView
    │   └── toolcard/
    │       ├── ToolCardRecognizer.kt   ← 启发式拆分 Text / Tool 段
    │       ├── ToolCard.kt             ← 嵌入卡片
    │       ├── ToolCardFullScreen.kt   ← 全屏预览（WebView + pointerInteropFilter）
    │       ├── HtmlViewport.kt         ← v4.2.4 注入 viewport meta
    │       ├── FavoriteToolDialog.kt
    │       └── ToolSharer.kt           ← FileProvider + ACTION_SEND
    ├── settings/                       ← API Profile 管理
    └── theme/
```

---

## 二、接入 Proactive Agent 的扩展方案

### 2.1 目标形态

```
┌────────────────────────┐        ┌────────────────────────┐
│  PC（你的电脑）        │        │  Android App           │
│                        │        │                        │
│  ┌──────────────────┐  │        │  ┌──────────────────┐  │
│  │  Proactive Agent │  │        │  │  Agent Client    │  │
│  │  - LLM           │◄─┼────────┼─►│  - WebSocket     │  │
│  │  - Tools         │  │ WS/HTTP│  │  - 显示 agent 消息│  │
│  │  - 主动触发      │  │        │  │  - 推送通知      │  │
│  │  - 定时任务      │  │        │  │                  │  │
│  └──────────────────┘  │        │  └──────────────────┘  │
└────────────────────────┘        └────────────────────────┘
```

Agent 像 hermes 接飞书那样"作为一个外部用户/服务"接入 App，能：
1. **被动响应**：用户在 App 里发消息 → 路由到 Agent → Agent 用 LLM+Tools 回 → 推回 App 显示
2. **主动触发**：Agent 自己决定要说话（cron / 文件监视 / 外部事件）→ 推到 App → 显示 + 系统通知

### 2.2 三种接入架构对比

| 方案 | 描述 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. 反向 WebSocket** | App 主动连 PC 的 WS server，长连接 | 不需要公网 IP；实时双向 | PC 必须先启动；锁屏断网时连接会断 |
| **B. FCM 推送 + 轮询** | PC 调 FCM API 推通知，App 唤醒后拉数据 | 国产 ROM 也能唤醒（有 GMS 时） | 需要_google-services.json；无 GMS 的设备（华为）不工作 |
| **C. MQTT broker** | 双方都连到 MQTT broker（自部署或 EMQX） | 真正异步、断线重连成熟、QoS 可选 | 多一个组件；要部署 broker |
| **D. HTTP 长轮询** | App 定期 GET PC 的 `/poll` 端点 | 实现最简单 | 实时性差；电池有损耗 |

**推荐：A + C 混合**
- 局域网 / 同 VPN 下：用 A（反向 WebSocket）。PC 启个 WS server（fastapi / socket.io / Node ws），App 启动后连过去。
- 跨网 / PC 没公网：用 C（MQTT）。两边都连 broker，broker 跑在你的 VPS 上。

下面以 **方案 A（反向 WebSocket）** 为例说明改动点。

### 2.3 PC 端 Agent 框架（推荐栈）

```
proactive-agent/
├── pyproject.toml
├── server.py                # FastAPI + WebSocket
├── agent/
│   ├── core.py              # 主循环：LLM + Tool calling
│   ├── memory.py            # 会话记忆 / 长期记忆
│   ├── scheduler.py         # APScheduler，定时主动触发
│   └── tools/               # 文件系统、shell、网页抓取、HTTP API 调用
└── config.yaml              # 端口、LLM key、触发规则
```

参考你之前的 NeuroForgeAgent 框架（35 Features 已完成）做底座，把它的 transport 层从 CLI 换成 WebSocket server。

### 2.4 Android 端改动清单

按从轻到重的依赖顺序：

#### 阶段 1：协议层（不改 UI）

**新增 `data/remote/AgentClient.kt`**

```kotlin
interface AgentClient {
    val incoming: Flow<AgentEvent>          // agent → app
    suspend fun send(msg: AgentOutbound)     // app → agent
    suspend fun connect(url: String, token: String)
    suspend fun disconnect()
}

sealed class AgentEvent {
    data class Reply(val text: String, val reasoning: String?) : AgentEvent()
    data class Notification(val title: String, val body: String) : AgentEvent()
    data class ToolCard(val html: String) : AgentEvent()
    object Heartbeat : AgentEvent()
}

data class AgentOutbound(
    val conversationId: String,
    val text: String,
    val attachments: List<String> = emptyList()
)
```

**实现：OkHttp WebSocket + 自动重连**

```kotlin
@Singleton
class OkHttpAgentClient @Inject constructor() : AgentClient {
    private val _incoming = MutableSharedFlow<AgentEvent>(extraBufferCapacity = 64)
    override val incoming = _incoming.asSharedFlow()
    private var ws: WebSocket? = null

    override suspend fun connect(url: String, token: String) {
        // 用和 LlmProviderFactory 一样的 sharedConnectionPool 思路
        // 每 30s 发心跳；断线指数退避重连（参考 OpenAiCompatibleProvider 重试）
    }
    // ...
}
```

#### 阶段 2：服务层（前台服务，复用 :streaming 进程模式）

**新增 `service/AgentService.kt`**（仿照 StreamingService）：

```kotlin
@AndroidEntryPoint
class AgentService : Service() {
    @Inject lateinit var agentClient: AgentClient

    override fun onStartCommand(intent: Intent?, ...) {
        startForeground(AGENT_NOTIF_ID, buildNotification())  // ← 关键
        serviceScope.launch {
            agentClient.connect(url, token)
            agentClient.incoming.collect { event -> handleEvent(event) }
        }
    }

    private suspend fun handleEvent(event: AgentEvent) {
        when (event) {
            is AgentEvent.Reply -> {
                // 写入 Room，UI 自动刷
                repository.appendMessage(Message(role = ASSISTANT, content = event.text, ...))
            }
            is AgentEvent.Notification -> {
                NotificationManager.notify(...)
            }
            // ...
        }
    }
}
```

**Manifest 改动**：

```xml
<service
    android:name=".service.AgentService"
    android:exported="false"
    android:process=":agent"                           ← 第三个进程
    android:foregroundServiceType="dataSync" />
```

可选：把 AgentService 跑在 `:streaming` 同一进程，省一个进程开销，但耦合度更高。建议先分进程，跑通后再合并。

#### 阶段 3：UI 接入

**ChatViewModel 新增 agent 模式标志**：

```kotlin
data class ChatUiState(
    // ...
    val activeMode: ChatMode = ChatMode.LLM,    // LLM | AGENT
)

fun sendMessage(text: String, attachments: List<Attachment>) {
    when (_uiState.value.activeMode) {
        ChatMode.LLM -> { /* 现有逻辑：StreamingService */ }
        ChatMode.AGENT -> {
            // 把消息写进 Room（user 角色立即显示）
            repository.appendMessage(userMessage)
            // 通过 WebSocket 发给 agent，等 AgentEvent.Reply 回来
            agentClient.send(AgentOutbound(convId, text))
        }
    }
}
```

**TopAppBar 加切换**：

```kotlin
actions = {
    IconToggleButton(
        checked = uiState.activeMode == ChatMode.AGENT,
        onCheckedChange = { viewModel.toggleMode() }
    ) {
        Icon(Icons.Filled.SmartToy, ...)
    }
    // ...
}
```

#### 阶段 4：配置页

`SettingsScreen` 加一个 "Agent 服务" 区块：
- WS server URL
- Auth token
- 心跳间隔
- 启用/禁用开关

复用现有 `ApiProfileDao` 的模式新建 `AgentProfileDao`（其实 DB 里已经有 `AgentProfileEntity` 占位了——见 `AppDatabase.entities` 列表）。

### 2.5 与现有架构的契合点

| 现有组件 | 复用方式 |
| --- | --- |
| `StreamingService` 模式（多进程 + FGS + WakeLock + Map<id,Job>） | 直接抄一份做 `AgentService` |
| Room 多实例失效 | agent 进程写入 → main 进程 observer 自动收到，无需手动 IPC |
| `StreamAiReplyUseCase` 的"先 appendMessage 占位、流式更新、最后 finalize"模式 | AgentClient.Reply 直接 appendMessage 即可；reasoning 字段已存在 |
| `MessageBubble` / `MarkdownText` / `ToolCardRecognizer` | 不动，agent 回复走同样的渲染路径 |
| `FavoriteToolRepository` / 收藏 / 分享 | 完全复用，agent 推的工具卡片也能收藏分享 |
| `LlmProviderFactory` 的 SSE 重试 + OkHttp connection pool | 抄一份给 `OkHttpAgentClient` 的重连逻辑用 |

### 2.6 推荐落地顺序

1. **第 1 周**：PC 端 agent 框架（基于 NeuroForgeAgent 改 transport，写一个 FastAPI WS server，先实现被动响应 + 一个 echo tool）
2. **第 2 周**：Android `AgentClient`（OkHttp WebSocket）+ 单元测试（mock WS）
3. **第 3 周**：`AgentService` + Manifest 改动 + 启动时连接、断线重连
4. **第 4 周**：ChatViewModel 接 agent 模式 + UI 切换；打通"App 发 → Agent 回 → 显示"
5. **第 5 周**：主动触发（agent scheduler）+ 推送通知（NotificationManager + 前台服务）
6. **第 6 周**：把 tool card 收藏 / 分享 / 全屏预览都接到 agent 上；接入 file tool（agent 把生成的文件丢给 app 查看）

### 2.7 可能的坑

- **OkHttp WebSocket 在锁屏下断连**：和 SSE 一样，靠 foreground service + wakeLock。`:agent` 进程可以和 `:streaming` 共用 StreamingWakeLock 模式。
- **消息去重**：agent 重连后可能重发最后几条消息。给每条 AgentEvent 加 `eventId`，App 用 `messageDao.insert` 的 ABORT 策略（同 id 不覆盖）做幂等。
- **国产 ROM 杀后台**： agent 进程跑前台通知；电池优化白名单；引导用户锁屏锁定后台。
- **本地调试 vs 生产**：本地 agent 跑 `ws://192.168.x.x:8000`；生产建议加 TLS + token，跑在自己的 VPS 上。

---

## 三、版本演进时间线

| 版本 | 主要变化 |
| --- | --- |
| v1.0 | WSL2 编译通过，11 文件骨架 |
| v2.0 | ViewModel + Flow + Room，4 supplier |
| v3.0 | 附件 / 语音 / 多模态 |
| v4.0 | 双进程 + Room 唯一真相源 + 多 session 并发 |
| v4.1 | UX 增强（点赞点踩 / 重试 / 长按菜单） |
| v4.2 | 工具卡片（拆分 / 收藏 / 分享 / 全屏） |
| v4.2.1 | AI 回复去气泡感 + 工具全屏 Sheet→Dialog |
| v4.2.2 | SSE 重试 + 严格贴底自动滚 + 思考粘顶 |
| v4.2.3 | 浏览器感工具全屏 + 分享行 + 电池提醒去重 |
| v4.2.4 | LastConversationTracker + HTML viewport + 长按菜单完整化 |
| v4.2.5 | 移除 deleteMessage 分支（永不删 AI 占位行） |
| **v4.2.6** | **updatedAt 字段 + 只清 2 分钟以上孤儿 STREAMING 行** |
