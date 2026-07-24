# 统一模型网关 API 规范 (AiChat Gateway v1)

**目的**：在客户端（安卓 App）与上游模型（ASR/TTS/LLM Agent/文生图/图生图）之间定义一份**统一**的 REST + SSE 协议。客户端只对接这份协议，所有上游厂商差异（OpenAI / Anthropic / GLM / 通义 / 自家模型）都由网关在后端抹平。

> 类比：OpenAI 官方 API 是事实标准，但缺少 ASR/TTS/T2I/I2I 等模态的统一封装，且各家厂商字段名各不相同。本规范在 OpenAI 基础上**扩展**多模态、统一字段名、统一 SSE 事件。

---

## 0. 设计原则

1. **OpenAI 兼容**：默认走 `/v1/chat/completions` 协议，能直接代理给 OpenAI/GLM/通义/DeepSeek/Kimi 等。
2. **parts 化消息**：`content` 一律数组化，每个 part 带 `type`（text / image / audio / file），告别 OpenAI 的 `image_url` vs Anthropic 的 `source` 字段名歧义。
3. **SSE 事件类型扩展**：OpenAI 只有 `delta.content`；本规范增加 `reasoning_delta` / `audio_delta` / `search_results` / `tool_call` / `image` 等事件，覆盖所有模态流式输出。
4. **同一会话多模态**：一次请求里可以同时带文本、图片、音频附件；网关拆分给上游模型（文本+图片给 LLM，音频先转 ASR 再给 LLM）。
5. **认证统一**：`Authorization: Bearer <token>` 一把钥匙开所有门。

---

## 1. 基础约定

### 1.1 Base URL
- 生产：`https://gateway.example.com/v1/`（占位，后期改成真实域名）
- 本地开发：`http://10.0.2.2:8080/v1/`（Android 模拟器访问宿主机）

### 1.2 认证
所有接口都需在请求头携带：
```
Authorization: Bearer <API_KEY>
Content-Type: application/json
Accept: text/event-stream  # 流式接口
Accept-Encoding: identity  # 禁用 gzip，保证 SSE 实时性
```

`<API_KEY>` 由网关签发，可在网关后台管理界面查看/重置。

### 1.3 错误格式
统一 OpenAI 风格：
```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "model_not_found",
    "message": "The model 'glm-99' does not exist",
    "param": "model"
  }
}
```

HTTP 状态码：400 参数错误 / 401 未授权 / 404 路径不存在 / 429 限流 / 500 服务端错误 / 503 模型不可用。

---

## 2. 统一消息格式 (parts-based)

### 2.1 消息结构
所有接口（chat / asr / agent）的请求体里的 `messages` 字段统一使用如下格式：

```json
{
  "role": "user",                      // user / assistant / system / tool
  "content": [
    {
      "type": "text",
      "text": "这张图里是什么？"
    },
    {
      "type": "image",
      "image_url": "https://...或 data:image/jpeg;base64,...",
      "detail": "high"                 // low / high / auto (可选)
    },
    {
      "type": "audio",
      "audio_url": "data:audio/mp4;base64,...",
      "format": "mp4"                  // mp4 / wav / m4a / opus
    },
    {
      "type": "file",
      "file_url": "https://...或 data:application/pdf;base64,...",
      "filename": "report.pdf",
      "mime_type": "application/pdf"
    }
  ]
}
```

### 2.2 part 类型

| type | 必填字段 | 可选字段 | 说明 |
|---|---|---|---|
| `text` | `text` | — | 文本片段 |
| `image` | `image_url` | `detail` | 图片（http URL 或 base64 data URL） |
| `audio` | `audio_url` | `format`, `sample_rate`, `language` | 音频（同上） |
| `file` | `file_url` | `filename`, `mime_type` | 通用文件附件 |
| `tool_result` | `tool_call_id`, `output` | — | 工具调用结果回传 |

### 2.3 网关侧转换
网关收到 parts 后：
- 图片 part → 翻译成 OpenAI 的 `{type:"image_url", image_url:{url}}` 或 Anthropic 的 `{type:"image", source:{...}}`
- 音频 part → 走 ASR 流水线转成 text，再插入到 messages
- 文件 part → PDF/Word 抽取文本，图片附件转 image_url，二进制上传到对象存储

---

## 3. 接口清单

| 路径 | 方法 | 用途 | 流式 |
|---|---|---|---|
| `/v1/chat/completions` | POST | LLM 文本/多模态对话 | 是 (SSE) |
| `/v1/asr` | POST | 语音转文字 | 否（短音频）/ 是（长音频） |
| `/v1/tts` | POST | 文字转语音 | 是 (SSE，分块返回音频) |
| `/v1/images/generations` | POST | 文生图 | 否 |
| `/v1/images/edits` | POST | 图生图 / 编辑图 | 否 |
| `/v1/agent/run` | POST | Agent 执行（工具调用 + 多轮） | 是 (SSE) |
| `/v1/models` | GET | 列出网关支持的模型 | 否 |
| `/v1/files` | POST/GET | 文件上传 / 查询 | 否 |

---

## 4. `/v1/chat/completions` — LLM 对话

### 4.1 请求
```json
{
  "model": "glm-4-plus",               // 网关路由的模型 ID
  "messages": [ /* 见 §2 */ ],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 2048,
  "top_p": 0.9,

  // 网关扩展字段
  "enable_reasoning": true,            // 显式要求 reasoning_content（R1/o1/Qwen3）
  "enable_rag": false,                 // 是否走 RAG 检索（需绑定知识库）
  "rag_collection": null,              // 知识库 ID
  "agent_id": null,                    // 绑定的 Agent profile（见 §8）
  "tools": [],                         // OpenAI tools schema
  "tool_choice": "auto"
}
```

### 4.2 SSE 事件类型

响应 `Content-Type: text/event-stream`，每个事件 `event: <type>` + `data: <json>`：

| event | data 字段 | 说明 |
|---|---|---|
| `text_delta` | `{delta:"..."}` | 文本增量（等同 OpenAI `choices[0].delta.content`） |
| `reasoning_delta` | `{delta:"..."}` | 思考过程增量（R1/Claude 3.7/Qwen3） |
| `audio_delta` | `{delta:"<base64>", format:"mp3"}` | TTS 流式音频块 |
| `image` | `{url:"...", b64:"..."}` | 文生图结果（最终 URL 或 base64） |
| `search_results` | `{results:[{title,snippet,url,score}]}` | RAG 检索到的引用 |
| `tool_call` | `{id, name, args}` | 模型发起工具调用 |
| `tool_result` | `{id, output}` | 工具执行结果回传给客户端（可选） |
| `usage` | `{prompt_tokens, completion_tokens, reasoning_tokens, duration_ms}` | token 计数（仅最后一条） |
| `finish` | `{reason:"stop"|"length"|"tool_calls", model:"..."}` | 流结束 |
| `error` | `{code, message}` | 流中错误 |

### 4.3 示例 SSE 流

```
event: text_delta
data: {"delta":"你好"}

event: text_delta
data: {"delta":"，有什么"}

event: reasoning_delta
data: {"delta":"用户用中文打招呼..."}

event: search_results
data: {"results":[{"title":"docs","snippet":"...","url":"https://...","score":0.92}]}

event: text_delta
data: {"delta":"可以帮你的？"}

event: usage
data: {"prompt_tokens":12,"completion_tokens":8,"reasoning_tokens":15,"duration_ms":1430}

event: finish
data: {"reason":"stop","model":"glm-4-plus"}
```

### 4.4 与 OpenAI SSE 的映射
网关可同时兼容老客户端（OpenAI SSE 格式）：
- OpenAI `data: {"choices":[{"delta":{"content":"..."}}]}` → 网关识别并转发为 `text_delta`
- 网关新增 `event:` 行让客户端按类型分发

---

## 5. `/v1/asr` — 语音转文字

### 5.1 请求（multipart 或 JSON）

**JSON（短音频，<25MB）**：
```json
{
  "audio_url": "data:audio/mp4;base64,...",
  "format": "mp4",
  "model": "whisper-large-v3",
  "language": "zh",                    // 可选，不传自动检测
  "prompt": "聊天,提问",               // 可选，引导识别词
  "response_format": "json"            // json / verbose_json / text / srt
}
```

**multipart（推荐，长音频）**：
```
POST /v1/asr
Content-Type: multipart/form-data
file: <binary>
model: whisper-large-v3
```

### 5.2 响应
```json
{
  "text": "你好，今天天气怎么样？",
  "language": "zh",
  "duration": 2.4,
  "segments": [
    {"start": 0.0, "end": 2.4, "text": "你好，今天天气怎么样？"}
  ]
}
```

### 5.3 流式 ASR（可选）
对实时麦克风输入，用 WebSocket `wss://gateway.example.com/v1/asr/stream`（未来 v2）。

---

## 6. `/v1/tts` — 文字转语音

### 6.1 请求
```json
{
  "model": "doubao-tts",
  "input": "你好，欢迎使用语音合成。",
  "voice": "female_warm",              // 音色 ID
  "response_format": "mp3",            // mp3 / wav / opus / pcm
  "speed": 1.0,                        // 0.5-2.0
  "stream": true                       // 流式分块返回
}
```

### 6.2 流式响应（SSE）
```
event: audio_delta
data: {"delta":"<base64-chunk>","format":"mp3"}

event: audio_delta
data: {"delta":"<base64-chunk>","format":"mp3"}

event: finish
data: {"duration":3.1,"format":"mp3"}
```

### 6.3 非流式响应
直接返回二进制 `audio/mp3` body。

---

## 7. `/v1/images/generations` + `/v1/images/edits`

### 7.1 文生图
```json
{
  "model": "doubao-t2i",               // 或 "kolors", "cogview-3", "dall-e-3"
  "prompt": "一只柴犬穿着宇航服，赛博朋克风格",
  "negative_prompt": "blurry, lowres",
  "n": 1,
  "size": "1024x1024",                 // 1024x1024 / 1024x1792 / 1792x1024
  "quality": "standard",               // standard / hd
  "style": "vivid",                    // vivid / natural
  "response_format": "url"             // url / b64_json
}
```

### 7.2 图生图 / 编辑
```json
{
  "model": "doubao-i2i",
  "prompt": "把背景换成赛博朋克城市夜景",
  "image": "data:image/png;base64,...",  // 原始图片
  "mask": "data:image/png;base64,...",   // 编辑蒙版（透明区域为待重绘区）
  "strength": 0.7,                       // 0-1，对原图的修改力度
  "n": 1,
  "size": "1024x1024"
}
```

### 7.3 响应
```json
{
  "created": 1735900800,
  "data": [
    {"url": "https://..."},
    {"url": "https://..."}
  ]
}
```

---

## 8. `/v1/agent/run` — Agent 执行

复杂任务走 Agent 接口，网关内部编排多轮 LLM 调用 + 工具调用。

### 8.1 请求
```json
{
  "agent_id": "code-expert",            // 预设 Agent ID（系统提供）
  "messages": [ /* 见 §2 */ ],
  "max_iterations": 10,                 // Agent 最大迭代次数
  "stream": true,

  // 工具白名单（覆盖 agent 配置）
  "tools": [
    {"type":"function","function":{"name":"web_search","parameters":{...}}}
  ],
  "tool_choice": "auto"
}
```

### 8.2 SSE 事件
继承 §4.2 所有事件，并增加：
- `tool_call`：Agent 发起工具调用
- `tool_result`：网关执行工具后的结果（客户端可见，便于调试）

---

## 9. `/v1/files` — 文件上传

长文件、视频、大图片不走 base64，先上传到网关对象存储，拿到 URL 再丢给 chat。

```
POST /v1/files
Content-Type: multipart/form-data

file: <binary>
purpose: chat_attachment              // chat_attachment / rag_upload / voice_clone
```

响应：
```json
{
  "id": "file_abc123",
  "url": "https://cdn.example.com/files/abc123.pdf",
  "filename": "report.pdf",
  "mime_type": "application/pdf",
  "size": 1048576,
  "expires_at": 1736000000
}
```

---

## 10. `/v1/models` — 模型列表

```json
{
  "object": "list",
  "data": [
    {
      "id": "glm-4-plus",
      "object": "model",
      "category": "chat",              // chat / asr / tts / t2i / i2i / embedding
      "capabilities": ["streaming","multimodal","reasoning","tool_calling"],
      "context_window": 128000,
      "owned_by": "zhipu",
      "pricing": {"input":0.05,"output":0.05,"unit":"元/千tokens"}
    }
  ]
}
```

---

## 11. 安卓端预留接口

> 详见代码：`data/remote/gateway/GatewayClient.kt` + `GatewayDtos.kt` + `UnifiedMessage.kt`

Retrofit 接口（已写好 stub，待后续接入）：

```kotlin
interface GatewayClient {
    @POST("chat/completions")
    suspend fun chatStream(
        @Body req: UnifiedChatRequest,
        @Header("Authorization") auth: String
    ): ResponseBody  // SSE 流

    @POST("asr")
    suspend fun asr(@Body req: AsrRequest): AsrResponse

    @POST("tts")
    suspend fun tts(@Body req: TtsRequest): ResponseBody  // audio/mpeg

    @POST("images/generations")
    suspend fun generateImage(@Body req: ImageGenRequest): ImageResponse

    @POST("images/edits")
    suspend fun editImage(@Body req: ImageEditRequest): ImageResponse

    @POST("agent/run")
    suspend fun agentRun(
        @Body req: AgentRunRequest,
        @Header("Authorization") auth: String
    ): ResponseBody  // SSE 流

    @Multipart
    @POST("files")
    suspend fun uploadFile(
        @Part file: MultipartBody.Part,
        @Part("purpose") purpose: RequestBody
    ): FileUploadResponse

    @GET("models")
    suspend fun listModels(): ModelsResponse
}
```

---

## 12. 从客户端到网关的字段统一表

| 客户端字段（OpenAI 原生） | 网关字段（统一） | 说明 |
|---|---|---|
| `content: "text"` 或 `[{type,text},{type,image_url}]` | `content: [{type,text/image/audio/file,...}]` | parts 化 |
| `delta.content` (OpenAI SSE) | `event:text_delta {delta}` | 事件类型化 |
| `reasoning_content` (DeepSeek) | `event:reasoning_delta {delta}` | 事件类型化 |
| `tool_calls` | `event:tool_call {id,name,args}` | 事件类型化 |
| `audio` (OpenAI TTS audio 模式) | `event:audio_delta {delta,format}` | 事件类型化 |
| `data: [{url}]` (Images API) | `event:image {url,b64}` | 事件类型化 |

---

## 13. 路线图

| 版本 | 内容 | 安卓侧动作 |
|---|---|---|
| **v1.0** | `/v1/chat/completions`（OpenAI 兼容） | **已完成**（v3 之前的 LlmProviderFactory 直接对接） |
| **v1.1** | SSE 事件扩展（reasoning/audio/image/search_results） | 现已支持 reasoning + search_results 解析 |
| **v1.2** | `/v1/asr` + `/v1/tts` | 接入 `GatewayClient.asr/tts`，替换内置 SpeechRecognizer |
| **v1.3** | `/v1/images/generations` + `/v1/images/edits` | 新增"画图"入口，输入栏 ⊕ 加"生成图片" |
| **v2.0** | `/v1/agent/run`（完整 Agent 编排） | 抽屉 Agent 切换器接入，Agent 列表从网关拉取 |
| **v2.1** | `/v1/files`（大文件上传） | 替换 base64 data URL，支持任意大小附件 |
| **v2.2** | `/v1/asr/stream`（WebSocket 实时 ASR） | 替换 SpeechRecognizer，支持持续录音 |

---

## 14. 变更记录

- **2026-07-24** 初版（v3 同步交付）。客户端预留 `GatewayClient` Retrofit 接口；现状代码仍走 OpenAI 兼容路径，网关协议 v1.1 落地后切换。
