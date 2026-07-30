# AiChat Android

OpenAI 兼容的 AI 聊天 Android 客户端，支持自定义 API 后端（Ollama / z.ai / OpenAI / PC agent 桥接），含 Python agent 框架服务端（paradise）。

## 项目结构

```
android-app/
├── app/                    # Android 客户端 (Kotlin + Compose + Hilt)
│   └── src/main/java/com/example/aichat/
│       ├── data/           # 数据层: Room DB, API, Mapper, Repository
│       ├── domain/         # 领域层: Models, UseCase, Repository interface
│       ├── di/             # Hilt DI 模块
│       ├── service/        # :streaming 进程 StreamingService
│       ├── ui/             # Compose UI: chat, settings, navigation, theme
│       ├── feature/        # 功能页面: 设置首页, API配置, Placeholder
│       └── util/           # 工具: CrashReporter, HonorOemHelper
├── server/                 # Python agent 服务端 (FastAPI + paradise)
│   ├── paradise/           # Agent 框架 (52 py, ~9100行)
│   ├── api/routes/         # 路由: openai_proxy (Ollama 代理)
│   ├── main.py             # 入口
│   ├── requirements.txt    # 依赖
│   └── start.sh            # 一键启动
├── docs/                   # 文档: API Gateway Spec, Bug Catalog
├── DEVELOPMENT_LOG.md      # 版本开发日志
└── REVIEW.md               # Code review 记录
```

## Quickstart

### Android 客户端

**前置条件**：Android Studio Hedgehog+ / JDK 17 / Gradle 8.7

```bash
cd android-app
./gradlew assembleDebug
# APK 输出: app/build/outputs/apk/debug/app-debug.apk
```

安装后进入 **设置 → 模型 API 配置**，新增一个 API profile：
- 供应商：**Custom** (OpenAI 兼容)
- Base URL：Ollama `http://<PC_IP>:11434/v1` 或直接指向 server
- API Key：Ollama 可不填
- 模型名：`qwen2.5:3b`

### Python Server (可选)

用于把 PC 上的 Ollama 暴露给手机（通过 ZeroNews/Tailscale/局域网）：

```bash
cd android-app/server
pip install -r requirements.txt    # 首次
bash start.sh                       # 启动 :8000
```

Ollama 需先安装并拉取模型：
```bash
ollama pull qwen2.5:3b
```

## 核心机制

### 双进程拓扑

```
┌─ 主进程 (com.example.aichat) ────────────────────┐
│  Compose UI + ChatViewModel                       │
│  Room DB (读写，与 :streaming 共享)                 │
└───────────────────────────────────────────────────┘
         │ Room multi-instance invalidation (~100ms)
┌─ :streaming 进程 ────────────────────────────────┐
│  StreamingService (foregroundServiceType=dataSync)│
│  StreamAiReplyUseCase + OkHttp SSE                │
│  Map<convId, Job> 多 session 并发                 │
│  MAX_CONCURRENT_STREAMS = 5                       │
└───────────────────────────────────────────────────┘
```

### 消息状态机

```
          ┌── useCase 插入 placeholder
          ▼
      STREAMING ──persist tick (150ms)──► STREAMING  (循环 ~7Hz)
               ├── Finish ──► COMPLETE
               └── Cancel/Error ──► INTERRUPTED / FAILED
```

### 设计原则

1. **Room 是唯一真相源**：UI 不从 ViewModel 维护"乐观状态"，所有 messages 来自 `observeMessages(convId): Flow<List<Message>>`
2. **多 session 并发**：ChatGPT 风格，切 session 不中断后台流；同 convId 定向 cancel，不同 convId 并行
3. **Foreground Service 无条件 startForeground**：Android 14+ 要求 `startForegroundService()` 后 5s 内调用，否则闪退

## API 端点 (Server)

| 端点 | 用途 |
|------|------|
| `GET /api/health` | 健康检查 |
| `POST /v1/chat/completions` | OpenAI 兼容聊天（streaming + non-streaming） |
| `GET /v1/models` | 模型列表 |

默认转发到 `localhost:11434` (Ollama)，可通过 `INFER_BASE_URL` 环境变量覆盖：
```bash
INFER_BASE_URL=http://localhost:8001 bash start.sh   # 指向 ORPO serve_orpo
```

## Server ↔ aipet-social 同步

paradise agent 框架来自 `/home/wwk/workspace/ai_project/aipet-social/` (commit `a8063ea`)。
当前 android-app 侧是开发分支，稳定后反向同步回 aipet-social。
详见 `SERVER_SYNC_TODO.md`。

## 构建技术栈

| 层 | 技术 |
|----|------|
| UI | Jetpack Compose (BOM 2024.x) |
| DI | Hilt + KSP (useKSP2=false) |
| DB | Room (v8, multiInstanceInvalidation) |
| 网络 | OkHttp + SSE (30s ping keep-alive) |
| 后台 | ForegroundService (dataSync type) |
| 加密 | Android Keystore (API key 存储) |
| Server | FastAPI + httpx + Ollama |
| Agent | Paradise Framework (hermes 模型) |
