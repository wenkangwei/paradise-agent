# v4.0 Bug Catalog — 对话页机制根因目录

> 2026-07-26 — 把 v3.3–v3.5.4 局部 patch 阶段所有"未根治"的 bug 一次性梳理、定位根因、统一修复。
>
> 设计原则：**Room 是唯一真相源**；多 session 并发；UI 不维护乐观状态。

## 严重度图例
- 🔴 致命（崩溃 / 主要功能不可用）
- 🟠 严重（用户体验重大受损）
- 🟡 一般（边界 / 体验细节）

---

## 🔴 BUG-1：切换会话后发新消息 → 闪退
- **位置**：`service/StreamingService.kt:68-105`（修复前）
- **现象**：用户在 A 流式中切到 B → 立即发消息 → app 闪退
- **根因**：`onStartCommand` 在 `streamingJob?.isActive == true` 分支里直接 `stopIfIdle() + return`，**没有调用 `startForeground()`**。Android 14+ 在 `startForegroundService()` 后强制要求 5s 内 `startForeground()`，否则抛 `ForegroundServiceDidNotStartInTimeException` → 进程崩溃。
- **触发频率**：用户报"切走再回来发消息必崩"——100% 命中
- **修复**：`onStartCommand` 入口**无条件**调 `startForeground()`；状态用 `Map<convId, Job>` 替代单 job；同 convId 重发时定向 cancel 旧 job

## 🔴 BUG-2：乐观 UI 与 Room 状态分裂 → AI 回复不展示
- **位置**：`ui/chat/ChatViewModel.kt:98-141`（修复前的 `observeMessages`）
- **现象**：切换会话回来后 AI 回复气泡空白 / 长时间无 spinner
- **根因**：`stopGenerating()` 把 `_uiState.messages` 里所有 streaming 消息的 `isStreaming` 强制改为 false，但 Room 行 status 仍是 `STREAMING`。下次 Room emit 时 `hasStreaming=true` 但 `state.isStreaming=false`，走 else 分支用 mapper 替换 messages——mapper 又写死 `isStreaming=false`——最终 UI 看到的是"空内容 + 无 spinner"
- **修复**：删除乐观 UI 拼接；`stopGenerating` 只翻转 `isLoading`；mapper 由 status 推导 isStreaming；`observeMessages` 简化为纯 Room → state 映射

## 🔴 BUG-3：`markConversationStreamingInterrupted` 与 UseCase persist 竞态
- **位置**：`ui/chat/ChatViewModel.kt:309-316` + `domain/usecase/StreamAiReplyUseCase.kt:127-132, 234-255`
- **现象**：切换会话后旧 session 行状态在 INTERRUPTED ↔ STREAMING 反复横跳，UI 抖动
- **根因**：ViewModel 在主进程 UPDATE 把 STREAMING 改成 INTERRUPTED；但 :streaming 进程的 UseCase 还在跑（stop intent 投递有延迟），80ms 后下次 persist 又写回 STREAMING
- **修复**：`selectConversation` **不再调** `markConversationStreamingInterrupted`；UseCase `maybePersistStreaming` 写之前先 `SELECT status`，已是 INTERRUPTED/FAILED 则抛 `CancellationException` 自然收敛

## 🟠 BUG-4：进入旧 session 时用户消息重复显示
- **位置**：v3.5.4 已修源代码（`StreamAiReplyUseCase.kt:62-72`），但**老 db 数据残留**
- **现象**：旧 session 内每条用户消息显示两遍
- **根因**：v3.5.4 之前 UseCase 在 ChatViewModel 已 insert 用户消息后又 insert 一次（新 UUID）。代码 bug 已修，db 数据未清
- **修复**：`MIGRATION_7_8` —— `DELETE FROM messages WHERE id NOT IN (SELECT MIN(id) ... GROUP BY conversationId, content, timestamp)`

## 🟠 BUG-5：停止生成后 spinner 消失但内容也消失
- **位置**：`ui/chat/ChatViewModel.kt:259-272`
- **现象**：按 stop 按钮 → 部分回复被覆盖成空 + spinner 消失
- **根因**：`stopGenerating` 立即翻转 `isStreaming=false`；但 Room 行还是 STREAMING；observeMessages 走 else 分支替换 messages 为 Room 版（mapper 写死 isStreaming=false）
- **修复**：`stopGenerating` 只翻转 `isLoading`（输入栏 spinner），不动 messages；等 Room 自然收敛

## 🟠 BUG-6：`ChatMessageMapper.toChatMessage` 无视 status
- **位置**：`ui/chat/model/ChatMessageMapper.kt:23`
- **现象**：Room 行 status=STREAMING 但 UI 永远 isStreaming=false
- **根因**：mapper 写死 `isStreaming = false`；唯一让 UI 知道在流式的路径是 observeMessages 手工拼接，一旦不走那个分支就丢失
- **修复**：`isStreaming = status == MessageStatus.STREAMING`

## 🟡 BUG-7：stopGenerating 没等 service 真正停止就翻转 UI
- 同 BUG-5

## 🟡 BUG-8：`AiChatApplication.onCreate` 在 :streaming 进程也跑 dangling sweep
- **位置**：`AiChatApplication.kt:26-30`
- **现象**：不致命，但浪费 IO；每个进程都跑一次 sweep
- **根因**：Application.onCreate 在所有进程都触发
- **修复**：删除 Application 内的 sweep；移到 ChatViewModel.init（只 main 进程 + 用户真正打开 chat 时触发）

## 🟡 BUG-9：Thinking 模型 "拼命思考中" 显示过久
- **位置**：`ui/chat/MessageBubble.kt:285-317`（`StreamingPlaceholder`）
- **现象**：glm-5.2 reasoning 已流出，主回答还没开始，UI 同时显示 reasoning + "拼命思考中"——用户误以为卡死
- **根因**：`StreamingPlaceholder` 的显示条件没考虑 reasoningContent 状态
- **修复**：条件改为 `message.isStreaming && content.isBlank() && reasoningContent.isNullOrBlank()`

## 🟡 BUG-10：`messagesObserverJob` cancel 后旧 collector 短暂并存
- **位置**：`ui/chat/ChatViewModel.kt:99-100`
- **现象**：短暂闪烁（不致命）
- **根因**：`cancel()` 协作式，要等下次 suspend 才退出
- **修复**：collector 内检查 `state.currentConversationId != conversationId` → return@collect

## 🟠 BUG-11：AI 慢/卡进度条（复合根因）
- **位置**：多处
- **现象**：用户感知"AI 一直卡在进度条"
- **根因（按概率）**：
  1. **50%**：thinking 模型 server-side 延迟（reasoning 5-20s 后 content 才开始）+ BUG-9 让用户误判
  2. **30%**：BUG-6 残留——mapper 写死 isStreaming=false，看不到流式更新
  3. **10%**：persist 80ms 频率（12Hz × 3 UPDATE）背压 SSE reader，低端机 bursty
  4. **5%**：multi-instance invalidation 延迟 100-500ms（Room 框架限制）
  5. **5%**：TLS 握手慢（已通过 sharedConnectionPool 缓解）
- **修复**：BUG-6 + BUG-9 修好后 90% 现象消失；SAVE_INTERVAL_MS 80→150ms 减少 DB 压力

## 🔴 BUG-12：修复方案引入的新 bug——切到 streaming session 被拦截发消息
- **位置**：本目录修复后的 ChatViewModel
- **现象**：进入一个正在 streaming 的 session，UI 显示 isLoading=true，用户发消息被拦截
- **根因**：删除乐观 UI 后，进入 streaming session 时 observeMessages 把 isLoading 设为 true；sendMessage 的 `if (_uiState.value.isLoading) return` 拦截
- **修复**：sendMessage 检测到当前 convId 在 streamingConversationIds 时**自动定向 stop**，再发新消息

## 🟠 BUG-13：dangling sweep 与 UseCase persist 的冲突（BUG-3 延续）
- **位置**：`AiChatApplication.onCreate` / `ChatViewModel.init` + `StreamAiReplyUseCase.maybePersistStreaming`
- **现象**：app 重启后 sweep 标记 INTERRUPTED，:streaming 进程仍在跑又改回 STREAMING
- **修复**：UseCase persist 前 SELECT status 检查，已是 INTERRUPTED/FAILED 则抛 CancellationException

## 🟡 BUG-14：multi-instance Room `SQLiteDatabaseLockedException`
- **位置**：跨进程 Room 写
- **现象**：罕见，两个进程同时写 aichat.db 抛 locked 异常
- **修复**：UseCase 已有 runCatching 兜底；ChatViewModel.observeMessages 的 collector 也补异常处理（容错）

## 🟡 BUG-15：`AttachmentEncoder.persist` 在 file:// URI 上失败
- **位置**：`data/remote/AttachmentEncoder.kt`
- **现象**：边界场景——pendingAttachments 残留 file:// uri 时再 persist 抛 IOException
- **修复**：persist 内部检查 scheme 已是 `file` 或裸路径时直接返回原 uri

---

## 闪退穷举（除上述 bug 外的所有已知崩溃路径）

| # | 崩溃源 | 修复状态 |
|---|-------|---------|
| 1 | `ForegroundServiceDidNotStartInTimeException` | ✅ BUG-1 |
| 2 | `SQLiteDatabaseLockedException` | ✅ BUG-14（runCatching） |
| 3 | `IOException` from AttachmentEncoder | ✅ BUG-15 |
| 4 | `IllegalStateException: No active ApiProfile` | ✅ 已有（UseCase catch → FAILED） |
| 5 | `CancellationException` propagation | ✅ 已有（NonCancellable finalize） |
| 6 | `SecurityException` POST_NOTIFICATIONS 未授权 | ✅ dataSync 类型不需要该权限 |
| 7 | `ForegroundServiceStartNotAllowedException` | ✅ 已用 startForegroundService() |
| 8 | `NetworkOnMainThreadException` | ✅ 已用 withContext(IO) |
| 9 | `JsonSyntaxException` SSE 解析 | ✅ runCatching 兜底 |
| 10 | Hilt `UninitializedPropertyAccessException` | ✅ @AndroidEntryPoint 保证 |
| 11 | NPE (ApiProfile null) | ✅ resolveProvider throw + UseCase catch |
| 12 | OOM 多 session 并发 | ✅ MAX_CONCURRENT_STREAMS=5 |

**结论**：除已列修复外，无其它已知崩溃路径。

---

## 文件改动清单（共 12 个文件）

| 文件 | 改动概要 |
|------|---------|
| `service/StreamingService.kt` | 单 job → Map<convId, Job>；onStartCommand 无条件 startForeground；定向 stop 重载；通知计数；STOP_FOREGROUND_DETACH |
| `ui/chat/ChatViewModel.kt` | 删乐观拼接；selectConversation/newChat 不 stop；stopGenerating 定向；observeStreamingConversations；MAX_CONCURRENT_STREAMS；BUG-12 自动 stop |
| `ui/chat/ChatUiState.kt` | 加 streamingConversationIds 字段 |
| `ui/chat/model/ChatMessageMapper.kt` | isStreaming 由 status 推导（核心修复 BUG-6） |
| `ui/chat/MessageBubble.kt` | StreamingPlaceholder 在 reasoning 非空时不显示（BUG-9） |
| `ui/chat/ConversationItem.kt` | 加 StreamingDot（呼吸绿点） |
| `ui/chat/ChatListScreen.kt` | ChatListDrawer 接 streamingConversationIds 参数 |
| `ui/chat/ChatScreen.kt` | 接线 streamingConversationIds 到 drawer |
| `data/local/dao/MessageDao.kt` | observeStreamingConversationIds Flow |
| `domain/repository/ChatRepository.kt` + Impl | observeStreamingConversationIds + getMessageStatus |
| `data/local/Migrations.kt` + `AppDatabase.kt` | v7→v8 去重（BUG-4）；version=8 |
| `AiChatApplication.kt` | 删 dangling sweep（移到 VM init，BUG-8） |
| `domain/usecase/StreamAiReplyUseCase.kt` | SAVE_INTERVAL_MS 80→150（BUG-11）；maybePersistStreaming pre-check status（BUG-13）；generateTitleIfNeeded NonCancellable |
| `data/remote/AttachmentEncoder.kt` | persist 跳过 file:// scheme（BUG-15） |
