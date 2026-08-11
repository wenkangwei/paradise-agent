# Live2D Stage B 实施记录：坑 + 待办

## 已完成

- [x] Stage A — Server edge-tts endpoint (`/api/tts/synthesize` + `/v1/audio/speech`)
- [x] Stage B — Live2D WebView 渲染 (pixi-live2d-display + Hiyori)
- [x] Stage C — UI 骨架 (HorizontalPager + InteractScreen + ImmersiveControlBar)
- [x] Stage D — 业务接线 (InteractViewModel + SingleShotChatUseCase + STT→LLM→TTS pipeline)
- [x] Stage E — 端到端真机验证

---

## 踩过的坑（按排查顺序）

### 1. `LAYER_TYPE_HARDWARE` 导致 canvas 不可见

**现象**：DOM 元素（div、border、text）正常显示，但 `<canvas>` 元素的绘制表面完全不可见——连 CSS 背景和 border 都不渲染。

**根因**：手动设 `setLayerType(LAYER_TYPE_HARDWARE)` 把整个 WebView 放到独立 GPU 纹理，canvas 子元素的 GPU surface 在这层纹理里合成失败。

**修复**：不手动设 LAYER_TYPE，用默认管线。App 默认已硬件加速，足够 pixi/WebGL。

### 2. 全屏 canvas 被静默丢弃 — tile memory budget 超限

**现象**：去掉 LAYER_TYPE_HARDWARE 后 canvas border 可见，但 canvas 内容（背景、2D 绘图、WebGL）全不渲染。小尺寸 canvas（300×300）正常。

**根因**：Chromium 的 `cc/tiles/tile_manager.cc` 瓦片内存预算有限。DPR=3 的手机全屏 canvas 缓冲区（~280 万像素）远超预算，Chromium **静默丢弃** canvas 瓦片。logcat 有 `WARNING: tile memory limits exceeded, some content may not draw`，但 Honor 设备 console 被隔离看不到。

**修复**：PIXI Application 设 `resolution: 1`（不乘 devicePixelRatio），canvas 缓冲区控制在 CSS 像素数（~30 万像素），远低于瓦片预算。

**诊断方法**：先测一个 300×300 小 canvas；如果小 canvas 显示、全屏 canvas 不显示，就是瓦片内存问题。

### 3. `pixi-live2d-display` bundle 路径

**现象**：`PIXI.live2d.Live2DModel` 是 undefined，报 `can't read properties of undefined reading registerTicker`。

**根因**：用了 `dist/index.min.js`，该 bundle 要求 Cubism 2 + Cubism 4 双 core。Hiyori 是 Cubism 4，应该用 `dist/cubism4.min.js`（只需 Cubism 4 core）。

**修复**：
```html
<script src="https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js"></script>
```

### 4. WebView 吞掉触摸事件 — 控制栏无法唤醒

**现象**：沉浸态下点击屏幕，控制栏（alpha 动画）不出现。

**根因**：WebView 是 Android View，内部 `onTouchEvent` 消费所有触摸事件。Compose 的 `pointerInput { detectTapGestures }` 挂在 AndroidView 的 modifier 上，永远收不到事件。

**修复**：在 WebView 之上（z-order 更高）加一层透明 Compose `Box` + `pointerInput` 拦截 tap，WebView 收不到触摸，Compose 层处理唤醒。

### 5. WebGL GPU 合成层盖住 HTML loading 蒙层

**现象**：HTML 里的 `#loading` 蒙层（spinner + 文字）不显示。

**根因**：WebGL canvas 被提升到独立 GPU 合成层，渲染在所有同层 DOM 元素之上。HTML loading 蒙层被 canvas GPU 层盖住。

**修复**：把 loading 指示器移到 **Compose 层**。JS 通过 `@JavascriptInterface` 通知 Kotlin 加载状态（`onLoading/onReady/onError`），Kotlin 在 Compose 层显示 `CircularProgressIndicator` overlay。

### 6. TTS URL 未配置 — 无声回复

**现象**：STT→LLM→文字回复正常，但没有语音播报。

**根因**：`VoiceConfig.ttsUrl` 默认空字符串，TTS 被静默跳过。用户配了 STT URL 但没配 TTS URL。

**修复**：在 `VoiceConfig` 加 `resolvedTtsUrl` 计算属性——从 `sttUrl` 的 host:port 自动推导 `/v1/audio/speech` 路径。同时 `resolvedTtsVoice` 把 OpenAI 默认音色 `alloy` 映射到 edge-tts 的 `zh-CN-XiaoxiaoNeural`。

### 7. `beyondBoundsPageCount` 在 Compose 1.6.x 导致首页白屏

**现象**：设 `beyondBoundsPageCount = 1` 让两个 page 都在 composition 中，首页（chat tab）变白屏。

**根因**：Compose 1.6.x（BOM 2024.06）的 `beyondBoundsPageCount` 有 layout bug。Compose 1.7+ 改名为 `beyondViewportPageCount` 并修复了。

**修复**：放弃 `beyondBoundsPageCount`，改用**进程级 WebView 缓存**（`WebViewCache` 单例）。WebView 创建一次后缓存，page 切换不 destroy，重进直接复用。

### 8. 每次切换 page 重新加载 WebView

**现象**：左滑到 chat tab → 右滑回 interact tab，WebView 重新加载 CDN + pixi + 模型（3-5 秒）。

**根因**：InteractScreen 离开 composition → `DisposableEffect.onDispose` → `webView.destroy()` → 模型丢失。

**修复**：`WebViewCache` object 持有 WebView 引用（ApplicationContext，不泄露 Activity）。`DisposableEffect` 只清 ref 不 destroy。下次进 page 时 factory 从 cache 取已存在的 WebView，JS 状态（pixi + 模型）都在，秒回。

---

## 待办（按优先级）

### P0 — 口型同步

模型已定义 `LipSync` 组（`ParamMouthOpenY`）。TTS 播放时用 `Visualizer`/`AudioTrack` 采样实时音量，每 ~50ms 调 `setParam('ParamMouthOpenY', amplitude)`，嘴巴跟着语音张合。不依赖 LLM，纯客户端增强，体验提升最大。

### P1 — LLM 直接参数控制

JS 侧暴露通用 setter：
```javascript
function setParam(name, value) {
  if (model) model.internalModel.coreModel.setParameterValueById(name, value);
}
```

LLM prompt 约束输出 JSON：`{"text": "你好！", "params": {"ParamAngleX": 15, "ParamEyeBallY": -0.3}}`

Kotlin 解析后逐个调 `evaluateJavascript("setParam('ParamAngleX', 15.0)")`。

可控制：头部朝向（AngleX/Y/Z）、眼神方向（EyeBallX/Y）、身体摆动（BodyAngleX/Y/Z）、眉毛（BrowX/Y）。

### P1 — 动作组控制

LLM 返回 `{"text": "...", "motion": "TapBody"}`，Kotlin 调 `playMotion('TapBody')`。

**限制**：Hiyori 只有 `Idle`（9 种）和 `TapBody`（1 种）。要更多动作得换模型或加自定义 `.motion3.json`。

### P2 — CDN 本地化

pixi.js / cubism core / pixi-live2d-display 从 CDN（jsdelivr / cubism.live2d.com）加载。首网慢或被墙时角色永远加载不出来。把三个 JS 文件打包进 `assets/live2d/lib/`，`<script src="lib/pixi.min.js">` 本地加载。

### P2 — config change 不丢 WebView

当前 `WebViewCache` 是 process-level，rotation（如果没锁）会 destroy Activity。WebView 用的是 ApplicationContext 所以不泄露，但 rotation 后需要重新 attach。可以在 `onConfigurationChanged` 或用 `rememberSaveable` 标记重 attach。

### P3 — 更丰富的模型

换有 `.exp3.json` 表情文件的模型（happy/angry/surprired/sad），或用 VTube Studio 的免费模型。需要兼容 Cubism 4 + pixi-live2d-display。

### P3 — 眼神追踪 / 跟脸

用前置摄像头 Face Detection 检测用户脸部位置，映射到 `ParamEyeBallX/Y` + `ParamAngleX/Y`，角色"看着"用户。Android `CameraX` + `FaceDetector`。

---

## 架构总结

```
用户 ─push to talk─> STT ─> LLM ─stream─> TTS ─> MediaPlayer
                                  │                    │
                                  ▼                    ▼
                            InteractViewModel    Live2D startSpeaking()
                                  │
                                  ▼
                         evaluateJavascript("playIdle()")
                                  │
                                  ▼
                    WebView (pixi-live2d-display)
                                  │
                                  ▼
                        Live2D Hiyori 渲染
```

**控制 API**：`evaluateJavascript()` 是唯一的 Kotlin→JS 通道。JS 能访问的一切（motions、expressions、parameters）都能从 Kotlin/LLM 控制。

**缓存层**：`WebViewCache` object → WebView 进程级持久化，CDN + 模型只加载一次。

**加载状态桥**：`@JavascriptInterface` (`AndroidBridge.onLoading/onReady/onError`) → Compose `Live2DLoadState` → LoadingOverlay。
