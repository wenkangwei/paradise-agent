# AiChat Android 开发日志

> 记录版本演进、踩过的坑、以及关键决策。新版本发布时追加一节。

---

## v3 — 2026-07-24（语音/Markdown/悬浮栏/统一网关）

### 背景
v2 装机后用户二次实测，提了 5 个问题。本版同时落地一个**重要架构决策**：
为后续自建统一模型网关（ASR/TTS/LLM/文生图/图生图）预留接口。

### Git 分支策略
按用户要求，从 v3 开始每版独立分支：
- `main` → v1 baseline (0b4bcff)
- `v2-baseline` → v2 改动提交点 (18628ff)
- `v3-voice-markdown-floating` → 本次 v3 所有改动

### 修复清单

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| 1 | 语音按钮一直按没反应 | 原方案用 `detectTapGestures(onPress=…)` + `tryAwaitRelease()`，在多次 recomposition 下不稳定（手势识别和 Compose 重组竞争） | 改用 Compose 官方推荐的 **InteractionSource + collectIsPressedAsState()** 模式：`MutableInteractionSource` → `LaunchedEffect(isPressed)` 触发 `onPress()/onRelease()` | `ChatInputBar.kt` |
| 2 | AI 回复还是裸 markdown | 之前的手写 parser 只处理 code block 和加粗 | **手写 AnnotatedString 渲染器**（约 350 行），支持 headers/bold/italic/strike/inline-code/` ``` ` 代码块/列表/blockquote/hr/link。代码块带 copy 按钮。先尝试用 `com.mikepenz:multiplatform-markdown-renderer-m3`，但该库**不在阿里云镜像**，国内拉不到，回退手写。思考内容用 `ReasoningSection.kt` 折叠卡片；RAG 结果用 `SearchResultsSection.kt` 折叠列表。 | `MarkdownText.kt`, `ReasoningSection.kt`, `MessageBubble.kt` |
| 3 | GLM 返回慢 | HttpLoggingInterceptor 读每一个 chunk + gzip 让 OkHttp 缓冲 SSE + 跨 profile 切换不共享 ConnectionPool | LoggingInterceptor 改 `NONE`；请求头加 `Accept-Encoding: identity` 禁用 gzip；`LlmProviderFactory` 加静态共享 `ConnectionPool(maxIdle=5, keepAlive=5min)` | `LlmProviderFactory.kt` |
| 4 | 输入栏有底栏分隔线、⊕ 和发送按钮在 TextField 下方 | Scaffold 的 `bottomBar` 产生的视觉割裂；按钮放在 Column 里 TextField 之下 | 删 `bottomBar`；整个输入栏用 `Box(Alignment.BottomCenter)` 浮在 content 上；TextField 用 `leadingIcon=⊕` / `trailingIcon=Stop/Mic/Send` 三态按钮，全部塞进胶囊内部。LazyColumn 加 `bottom=96.dp` 防止消息被遮挡 | `ChatScreen.kt`, `ChatInputBar.kt` |
| 5 | (架构)统一网关协议 | 用户后端要做统一网关（LLM+ASR+TTS+T2I+I2I），需要 App 预留接口 + 统一消息格式 | 新建 `docs/API_GATEWAY_SPEC.md` 规范文档（14 节）+ `data/remote/gateway/` 包（`UnifiedMessage.kt` parts-based 消息、`GatewayDtos.kt` 8 个端点 DTO、`GatewayClient.kt` Retrofit 接口 stub）；`BuiltinSuppliers` 新增 `MyGatewaySupplier` 入口 | 见下 |

### 统一网关设计要点（v3 #5）

**为什么**：OpenAI 兼容协议只覆盖 chat，但用户的网关要同时支持 ASR/TTS/文生图/图生图。各厂商字段名各不相同（`image_url` vs `source`、`delta.content` vs `reasoning_content`），客户端直连各家会变成长尾地狱。

**怎么做的**：
1. **消息 parts 化**：所有模态共用 `content: [{type:"text"/"image"/"audio"/"file"/"tool_result"}]`
2. **SSE 事件类型化**：不再靠 `delta.xxx` 判别，而是用 `event: text_delta / reasoning_delta / audio_delta / image / search_results / tool_call / tool_result / usage / finish / error`
3. **8 个端点**：`/v1/chat/completions`、`/v1/asr`、`/v1/tts`、`/v1/images/generations`、`/v1/images/edits`、`/v1/agent/run`、`/v1/files`、`/v1/models`
4. **客户端预留**：`GatewayClient` Retrofit 接口已经写好但**未注入 Hilt**，等后端起来改 `NetworkModule` + `ChatRemoteDataSource` 切流即可

### 踩过的坑（v3 新增）

1. **mikepenz 库不在阿里云镜像**
   - 报错：`Could not find com.mikepenz:multiplatform-markdown-renderer-m3:0.5.4`
   - 原因：阿里云只镜像 maven central/google，mikepenz 的发布在 central 但延迟或镜像策略导致没同步
   - 解法：删依赖，手写 AnnotatedString-based markdown renderer

2. **InteractionSource 比 detectTapGestures 稳定**
   - 原因：`detectTapGestures` 在 `pointerInput` 里跑，受 Compose 重组影响；`collectIsPressedAsState()` 通过 state 订阅更稳定
   - 注意：要给 Box 同时设 `clickable(interactionSource=…, indication=rememberRipple())`，否则按下没水波纹反馈

3. **SSE 禁用 gzip 的正确姿势**
   - 不是删 `Accept-Encoding` 头（OkHttp 会自动加），而是显式设 `Accept-Encoding: identity`
   - 否则 OkHttp 透明解压，但 chunked transfer-encoding + gzip 在很多 SSE 网关会**缓冲到流结束才发**，体感就是"等半天然后一次性出全部字"

4. **共享 ConnectionPool 节省握手**
   - 多 profile 切换时如果每次都新建 OkHttp client，TCP+TLS 握手重复
   - 静态 `ConnectionPool(maxIdle=5, keepAlive=5min)` 让 5 个 idle 连接跨 profile 复用
   - 但要注意：**不同 baseUrl 不复用**，连接池按 host 区分，所以只在同 domain 不同 path 时受益

### 构建命令备忘

```bash
# 编译 v3（在 WSL2 内）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export ANDROID_HOME=$HOME/Android/Sdk
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
cd /home/wwk/workspace/ai_project/android-app
git checkout v3-voice-markdown-floating
./gradlew assembleDebug --no-daemon

# 产物
cp app/build/outputs/apk/debug/app-debug.apk /mnt/c/Users/wenka/Desktop/aichat-v3-debug.apk
```

### v3 已知未做

- **ASR 走系统 SpeechRecognizer**（免费但识别质量一般）→ 等网关 `/v1/asr` 起来换 Whisper
- **TTS 未做** → 等网关 `/v1/tts`
- **文生图未做** → 等网关 `/v1/images/generations`
- **Agent 切换器** → 等网关 `/v1/agent/run`
- **base64 内联图片入库**：超大图片会让 Room 行撑爆，未来要走 `/v1/files` 上传后存 URL

### 文件清单（v3 新增/修改）

**新增**：
- `docs/API_GATEWAY_SPEC.md` — 统一网关规范（14 节）
- `app/src/main/java/com/example/aichat/data/remote/gateway/UnifiedMessage.kt`
- `app/src/main/java/com/example/aichat/data/remote/gateway/GatewayDtos.kt`
- `app/src/main/java/com/example/aichat/data/remote/gateway/GatewayClient.kt`
- `app/src/main/java/com/example/aichat/ui/chat/ReasoningSection.kt`

**重写**：
- `app/src/main/java/com/example/aichat/ui/chat/MarkdownText.kt`（删依赖改手写）
- `app/src/main/java/com/example/aichat/ui/chat/ChatInputBar.kt`（悬浮胶囊 + 内嵌按钮）
- `app/src/main/java/com/example/aichat/ui/chat/ChatScreen.kt`（移除 bottomBar）

**修改**：
- `app/src/main/java/com/example/aichat/data/provider/LlmProviderFactory.kt`（SSE 优化 + 共享池）
- `app/src/main/java/com/example/aichat/data/provider/BuiltinSuppliers.kt`（新增 MyGatewaySupplier）
- `app/src/main/java/com/example/aichat/domain/model/Models.kt`（MessageMetadata 加 searchResults）
- `app/src/main/java/com/example/aichat/ui/chat/model/ChatMessage.kt`（加 reasoningContent / metadata）
- `app/src/main/java/com/example/aichat/ui/chat/model/ChatMessageMapper.kt`（传播新字段）
- `app/src/main/java/com/example/aichat/ui/chat/MessageBubble.kt`（插入 ReasoningSection / SearchResultsSection）
- `gradle/libs.versions.toml` + `app/build.gradle.kts`（移除 markdown 库）

**产物**：`aichat-v3-debug.apk` (18 MB)

---

## v2 — 2026-07-24（用户首测后大修）

### 背景
v1 装机后用户实测提出 9 个问题，覆盖前端 UX、网络、视觉、功能完整性。
本版按优先级修复，APK 已产出 `aichat-v2-debug.apk` (18 MB)。

### 修复清单

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| 1 | 键盘弹出遮挡输入框 | 未设 `windowSoftInputMode`，输入栏也没用 imePadding | Manifest 加 `adjustResize` + Surface 加 `Modifier.imePadding().navigationBarsPadding()` | `AndroidManifest.xml`, `ChatInputBar.kt` |
| 2 | ⊕ 选拍照后没开摄像头 | 原代码 fallback 到相册（"留作后续"），且没 FileProvider | 配置 `FileProvider` + `<cache-path>` 资源 + 运行时申请 CAMERA 权限 + `TakePicture` 写入 `cacheDir/captures/` | `AndroidManifest.xml`, `res/xml/file_paths.xml`, `CaptureUriProvider.kt`, `ChatInputBar.kt` |
| 3 | GLM 等 URL 要用户手填，Key 无记忆 | BuiltinSuppliers 只有 7 个常用厂商；表单字段没有历史 | 补 8 家国内厂商预置（智谱/通义/Kimi/豆包/千帆/星火/零一/混元/SiliconFlow）+ URL/Key 历史下拉 | `BuiltinSuppliers.kt`, `ApiProfileRepository.kt`, `ApiConfigEditPage.kt`, `ApiConfigViewModel.kt` |
| 4 | 顶部 "AI Chat" 是死文本 | TopAppBar title 写死 | 改为 ProfileSelector（下拉显示所有 profile + 切换 active + 跳到管理页） | `ChatScreen.kt`, `ChatViewModel.kt`, `ChatUiState.kt` |
| 5 | 发送按钮永远是发送，没语音 | 原设计只有 Send/Stop 两态 | 三态：Stop（loading）/ Mic（空输入+空附件）/ Send。Mic 用 `SpeechRecognizer` 按住录音松开转译自动发送 | `VoiceInput.kt`, `ChatInputBar.kt` |
| 6 | GLM URL 自定义后 404 | `@POST("v1/chat/completions")` 与用户 baseUrl（含 `/v4/`）拼接成 `/v4/v1/chat/completions` | 改为 `@POST("chat/completions")`，依赖 baseUrl 自带版本段 | `AiApiService.kt` |
| 7 | 文字+附件挤在一个气泡 | MessageBubble 把 content 和 attachments 放同一 Surface | 拆成两个独立 Surface：上方附件气泡（图片网格 / 文件卡片），下方文字气泡 | `MessageBubble.kt`, `MessageAttachment.kt` |
| 8 | 文件附件气泡空白 | 只用 `AsyncImage` 渲染，非图片无 fallback | 按 mimeType 选 Material icon（PDF/Word/Excel/Video/Audio...）+ 显示文件名 + 类型标签 | `MessageAttachment.kt`, `AttachmentPreview.kt` |
| 9 | 软件没 icon | 前景 vector 用 `#00696D` 描线、背景同色 → 视觉上"隐形" | 重画 vector：白色气泡 + 三个 teal 对话点 + 黄色 ✨ AI 星标；背景换亮 teal `#00897B` | `ic_launcher_foreground.xml`, `ic_launcher_background.xml` |

---

### 踩坑经验（按主题分类）

#### A. Retrofit URL 拼接（#6 的根因，最值得记住）

**症状**：用户填了 `https://open.bigmodel.cn/api/paas/v4/`，404。

**原理**：
- Retrofit 2 的 `@POST("path")` 中 path 如果不带前导 `/`，就是相对路径，会和 baseUrl 字符串拼接。
- baseUrl 必须以 `/` 结尾（Retrofit 强制）。
- 用户填的 baseUrl `https://open.bigmodel.cn/api/paas/v4/` 已经包含版本段 `/v4/`。
- 旧代码 `@POST("v1/chat/completions")` → 最终 URL = `.../v4/v1/chat/completions` ❌ 双重版本段，必然 404。

**结论**：路径不要硬编码版本段，让 baseUrl 自己负责版本。所有 OpenAI 兼容端点都用 `@POST("chat/completions")`，URL 设计遵循 "baseUrl 必须以 `/v?/` 结尾"。

**验证清单**（添加新供应商时）：
```
GLM:        https://open.bigmodel.cn/api/paas/v4/    + chat/completions  ✓
OpenAI:     https://api.openai.com/v1/               + chat/completions  ✓
DeepSeek:   https://api.deepseek.com/v1/             + chat/completions  ✓
通义:        https://dashscope.aliyuncs.com/compatible-mode/v1/  + chat/completions  ✓
Kimi:       https://api.moonshot.cn/v1/              + chat/completions  ✓
```

#### B. Compose IME padding（#1 的根因）

**症状**：键盘弹出后盖住输入框，看不到打字内容。

**根因**：
- `ComponentActivity` 默认 `windowSoftInputMode=adjustResize` 不一定生效（尤其在 `enableEdgeToEdge()` 后）。
- 输入栏没有用 `imePadding()`，所以不知道 IME 高度。

**修复（三件套缺一不可）**：
1. **Manifest**：`android:windowSoftInputMode="adjustResize"` on MainActivity
2. **根 Scaffold/输入栏**：`.navigationBarsPadding()`（处理手势导航条）+ `.imePadding()`（处理键盘）
3. `enableEdgeToEdge()` 在 setContent 之前

**踩过的坑**：只加 `imePadding` 不加 Manifest 属性，部分 ROM 不生效；只加 Manifest 属性不加 `imePadding`，Compose 不知道要避让。

#### C. 摄像头 + FileProvider（#2 的根因）

**症状**：点拍照没反应（被 fallback 到相册）。

**完整链路**：
```
1. AndroidManifest 声明权限：<uses-permission android:name="android.permission.CAMERA" />
2. AndroidManifest 声明 FileProvider：
   <provider android:authorities="${applicationId}.fileprovider" ...>
       <meta-data android:resource="@xml/file_paths" />
   </provider>
3. res/xml/file_paths.xml 声明可分享的目录：
   <cache-path name="captures" path="captures/" />
4. 运行时生成 URI：
   FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", file)
5. ActivityResultContracts.TakePicture() 用这个 URI 启动系统相机
6. 拍完照，URI 指向的文件已经被相机写入 → 直接读
```

**关键经验**：
- **顺序很重要**：`cameraLauncher` 必须在 `cameraPermissionLauncher` **之前**声明，因为权限回调里要调 `cameraLauncher.launch()`。否则 Kotlin 报 `Unresolved reference`。
- **FileProvider authority** 必须用 `${applicationId}.fileprovider`，不能用硬编码的包名（debug/release 包名不同会崩）。
- **临时文件** 放 `cacheDir/captures/` 比 `externalCacheDir` 更稳，不受 SD 卡权限影响。
- **运行时权限** 不只是 CAMERA，麦克风要 RECORD_AUDIO。

#### D. SpeechRecognizer 用法（#5）

**症状**：要按住语音按钮录音，松开发送转译文字。

**实现要点**：
1. **不用第三方 SDK**：Android 内置 `android.speech.tts` 和 `SpeechRecognizer`，免费、离线（大部分现代手机通过 Google app 提供本地 ASR）。
2. **press-and-hold**：用 `Modifier.pointerInput { detectTapGestures(onPress = { ... }) }`，在 `onPress` 里 start，`tryAwaitRelease()` 后 stop。
3. **回调 threading**：`RecognitionListener` 在主线程回调；用 `mutableStateOf` 让 Compose 观察状态。
4. **结果处理**：`onResults` 拿 `RESULTS_RECOGNITION` 第一个，trim 后非空就调 onSend。空结果（用户按一下就松开没说话）不发。
5. **错误兜底**：`onError` 也要回调空字符串，否则按钮会卡在 listening 状态。
6. **DisposableEffect**：组件销毁时 `recognizer.destroy()`，否则泄露麦克风硬件。
7. **权限**：首次按 mic 如果没 RECORD_AUDIO，跳 `RequestPermission()` launcher；授权后再次按下才真正开始听。

**已知限制**：
- 个别国产 ROM 没有内置 ASR 服务，`SpeechRecognizer.isRecognitionAvailable()` 返回 false → 自动隐藏 mic 按钮，用户只能用文字输入。这是优雅降级。
- 不支持边按边显示转译（partial results），只支持松开后看结果。如果要做边录边显，需要监听 `onPartialResults` 并实时更新输入框。

#### E. Compose 组件设计

**陷阱 1：`by remember { mutableStateOf(...) }` 必须有 `setValue` import**
```kotlin
import androidx.compose.runtime.getValue   // 给 read 委托
import androidx.compose.runtime.setValue   // 给 write 委托（var 才需要）
```
一个 import 顺序错了能卡 10 分钟。

**陷阱 2：`Modifier.clickable` 全限定名链式**
```kotlin
// ❌ 错误：会编译失败
.androidx.compose.foundation.clickable { ... }

// ✓ 正确：加 import
import androidx.compose.foundation.clickable
.clickable { ... }
```

**陷阱 3：DropdownMenu 必须包在 Box 里，触发器加 `Modifier.menuAnchor()`**
```kotlin
ExposedDropdownMenuBox {
    OutlinedTextField(... modifier = Modifier.menuAnchor())
    DropdownMenu(...) { items }
}
```

**陷阱 4：`Alignment.End` vs `Alignment.TopEnd`**
```kotlin
// Box 的 contentAlignment: Alignment
// Column 的 horizontalAlignment: Alignment.Horizontal
// 这两个类型不互通，必须给 TopEnd 加显式类型标注：
val alignment: Alignment = if (isUser) Alignment.TopEnd else Alignment.TopStart
```

#### F. Hilt 注入（v1 留下的坑）

**症状**：`android.content.Context cannot be provided without an @Provides-annotated method`

**根因**：在 `@Inject constructor` 里直接写 `context: Context`，Hilt 不知道用 ApplicationContext 还是 ActivityContext。

**修复**：永远加 qualifier：
```kotlin
@Singleton
class ApiKeyEncryptor @Inject constructor(
    @ApplicationContext private val context: Context,  // ★ 必须
    ...
)
```

#### G. Adaptive Icon 的视觉陷阱（#9）

**症状**：用户说"软件没 icon"。

**根因**：原 vector 用 `fillColor="#00696D"` 画描线，背景色也是 `#00696D`，同色重叠 → 启动器里看上去就是个纯色方块。

**修复原则**：
- **前景和背景必须高对比**。推荐：白色前景 + 品牌色背景；或品牌色前景 + 浅色背景。
- **简单几何优于复杂插画**：108×108 viewport，但安全区只有中心 66×66（系统会裁剪边缘），复杂图案被裁后看不清。
- **mipmap-anydpi-v26** 够用，因为 minSdk 26+，不需要 bitmap fallback。

#### H. WSL2 编译工具链（v1→v2 通用）

唯一能同时工作的版本矩阵：
| 组件 | 版本 | 关键 |
|---|---|---|
| Kotlin | 2.0.21 | |
| KSP | 2.0.21-1.0.28 | |
| `ksp.useKSP2` | **false** | KSP2 有 jvm signature V bug |
| Hilt | 2.56.2 | 2.51.1 + Kotlin 2.0 有 AssistedFactory bug |
| Gradle | 8.7 wrapper | 走腾讯镜像绕开 WSL2 代理 SSL 问题 |
| AGP | 8.5.0 | |
| Room | 2.6.1 | |

**编译命令模板**：
```bash
cd /home/wwk/workspace/ai_project/android-app
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export ANDROID_HOME=$HOME/Android/Sdk
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
./gradlew assembleDebug --no-daemon 2>&1 | tail -50
```

**输出**：`app/build/outputs/apk/debug/app-debug.apk` → 复制到 `/mnt/c/Users/wenka/Desktop/aichat-vN-debug.apk`。

---

### 架构改进（v2 引入）

#### 1. ApiProfileRepository 的历史接口
```kotlin
suspend fun keyHistoryForSupplier(supplierId: String): List<String>
suspend fun urlHistory(supplierId: String? = null): List<String>
```
这两个方法返回**已解密的**历史 Key/URL，用于表单的下拉预填。Key 通过 `ApiKeyEncryptor.decrypt(id)` 解密——这意味着所有保存过的 Key 都是可恢复的（不是 hash），便于跨 profile 复用。**安全权衡**：方便 > 安全，因为这是个人设备上的本地存储。

#### 2. VoiceRecognizer 抽象
```kotlin
data class VoiceRecognizer(
    val start: () -> Unit,
    val stop: () -> Unit,
    val isListening: State<Boolean>,
    val isAvailable: Boolean
)
```
`rememberVoiceRecognizer(onResult)` 把 SpeechRecognizer 的 lifecycle 绑到 Composable。如果将来要换 Whisper API 或讯飞 SDK，只要实现同样的接口即可。

#### 3. ProfileSelector 实时反映 active 切换
通过 `combine(apiProfileRepo.observeAll(), apiProfileRepo.observeActive())` 在 ViewModel 监听两个 Flow，UI 自动跟随。切换 profile 不需要重启 App（这是 v1 修过的 Singleton Retrofit bug 的最终验证）。

---

### v3 待办（已知但未修）

- **App Icon 的 PNG fallback**：部分国产 ROM 启动器不识别 adaptive icon vector。下次要补 `mipmap-hdpi/mdpi/xhdpi/xxhdpi` 的 PNG。
- **语音边录边显**：当前只支持松开后看结果，不支持实时 partial results。
- **拍照后图片裁剪/压缩**：拍的 4000×3000 图直接入库会爆 DB。要加 Bitmap sampling。
- **附件大小限制**：现在没限，>10MB 的 PDF 会把内存撑爆。
- **多模态消息持久化**：当前 attachment 用 dataUrl 存数据库（base64 内联），图片大就胀库。未来应改成本地文件 + Room 只存路径。

---

## v1 — 2026-07-23（首版）

详见上一节 `aichat-v1-debug.apk`，包含：
- WSL2 编译环境从零搭建
- 11 个 Kotlin 编译错误的修复路径
- 4 个 Hilt/KSP 版本组合的试错
- 第一版 7 大模块骨架

完整记录见 `memory/aichat_build_success.md`。
