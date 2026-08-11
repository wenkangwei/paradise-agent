package com.example.aichat.feature.live2d

import android.annotation.SuppressLint
import android.content.Context
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/** Loading state reported by the WebView's JS pipeline. */
sealed class Live2DLoadState {
    data class Loading(val message: String) : Live2DLoadState()
    object Ready : Live2DLoadState()
    data class Error(val message: String) : Live2DLoadState()
}

/**
 * Compose wrapper around a WebView that renders a Live2D Cubism 4 model via
 * pixi-live2d-display. The model + renderer assets live under
 * `assets/live2d/`; this composable loads `assets/live2d/index.html`.
 *
 * Returns the current [Live2DLoadState] so the caller can show a Compose
 * loading overlay on top. The overlay MUST be a Compose layer (not an HTML
 * element) because the WebGL canvas gets its own GPU compositing layer that
 * paints over any sibling DOM elements.
 */
@Composable
fun Live2DView(
    phase: Live2DPhase,
    modifier: Modifier = Modifier,
): Live2DLoadState {
    val webViewRef = remember { mutableListOf<WebView?>() }
    var loadState by remember { mutableStateOf<Live2DLoadState>(WebViewCache.lastLoadState) }

    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            // Reuse the cached WebView if it was created in a previous page
            // visit. The cached WebView retains its loaded JS state (pixi,
            // model, textures), so swiping away and back doesn't trigger a
            // full reload. On first visit, create a new one and cache it.
            val wv = WebViewCache.getOrCreate(ctx) { msg ->
                webViewRef.firstOrNull()?.post { loadState = Live2DLoadState.Loading(msg) }
            }
            // Re-bind callbacks in case this is a new composable instance
            WebViewCache.bindCallbacks(
                onLoading = { msg -> webViewRef.firstOrNull()?.post { loadState = Live2DLoadState.Loading(msg) } },
                onReady = { webViewRef.firstOrNull()?.post { loadState = Live2DLoadState.Ready } },
                onError = { msg -> webViewRef.firstOrNull()?.post { loadState = Live2DLoadState.Error(msg) } },
            )
            // Sync load state from cache (model may already be loaded)
            if (WebViewCache.isReady) loadState = Live2DLoadState.Ready
            wv.also { webViewRef.add(0, it) }
        },
        update = { /* no-op — content driven by LaunchedEffect below */ },
    )

    LaunchedEffect(phase) {
        val w = webViewRef.firstOrNull() ?: return@LaunchedEffect
        when (phase) {
            Live2DPhase.Idle -> w.evaluateJavascript("if (typeof playIdle === 'function') playIdle();", null)
            Live2DPhase.Speaking -> w.evaluateJavascript("if (typeof startSpeaking === 'function') startSpeaking();", null)
            Live2DPhase.StoppedSpeaking -> w.evaluateJavascript("if (typeof stopSpeaking === 'function') stopSpeaking();", null)
        }
    }

    // Do NOT destroy the WebView on dispose — it stays cached for reuse.
    // The AndroidView framework automatically removes it from the View
    // hierarchy when this composable leaves composition.
    DisposableEffect(Unit) {
        onDispose {
            webViewRef.clear()
        }
    }

    return loadState
}

/** Coarse-grained motion state for the avatar. */
enum class Live2DPhase {
    Idle,
    Speaking,
    StoppedSpeaking,
}

/** Called from JS via `AndroidBridge.onLoading/onReady/onError`. */
interface Live2DBridge {
    fun onLoading(msg: String)
    fun onReady()
    fun onError(msg: String)
}

/** JS-accessible bridge; all methods fire on a binder thread. */
private class JsBridge(val cb: Live2DBridge) {
    @JavascriptInterface
    fun onLoading(msg: String) = cb.onLoading(msg)

    @JavascriptInterface
    fun onReady() = cb.onReady()

    @JavascriptInterface
    fun onError(msg: String) = cb.onError(msg)
}

/**
 * Process-level cache for the Live2D WebView. Keeps a single WebView instance
 * alive across page-swipe composition cycles so the CDN scripts + pixi model
 * load only once per app session.
 *
 * Threading: [bindCallbacks] is called from the main (composition) thread;
 * the callbacks are invoked from a binder thread (JavascriptInterface) and
 * post to the WebView's looper (main thread).
 *
 * Lifecycle: the WebView holds an application context (not Activity), so it
 * doesn't leak the Activity. It is never destroyed until the process dies.
 */
private object WebViewCache {
    @Volatile private var webview: WebView? = null
    @Volatile var isReady: Boolean = false
    @Volatile var lastLoadState: Live2DLoadState = Live2DLoadState.Loading("初始化中…")

    private var onLoadingCb: ((String) -> Unit)? = null
    private var onReadyCb: (() -> Unit)? = null
    private var onErrorCb: ((String) -> Unit)? = null

    fun getOrCreate(context: Context, onLoading: (String) -> Unit): WebView {
        return webview ?: synchronized(this) {
            webview ?: createWebView(context.applicationContext).also { wv ->
                webview = wv
            }
        }
    }

    fun bindCallbacks(
        onLoading: (String) -> Unit,
        onReady: () -> Unit,
        onError: (String) -> Unit,
    ) {
        onLoadingCb = onLoading
        onReadyCb = onReady
        onErrorCb = onError
    }

    private fun createWebView(appContext: Context): WebView {
        val bridge = object : Live2DBridge {
            override fun onLoading(msg: String) {
                lastLoadState = Live2DLoadState.Loading(msg)
                onLoadingCb?.invoke(msg)
            }
            override fun onReady() {
                isReady = true
                lastLoadState = Live2DLoadState.Ready
                onReadyCb?.invoke()
            }
            override fun onError(msg: String) {
                lastLoadState = Live2DLoadState.Error(msg)
                onErrorCb?.invoke(msg)
            }
        }
        return WebView(appContext).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = true
            settings.allowContentAccess = true
            @Suppress("DEPRECATION")
            settings.allowFileAccessFromFileURLs = true
            @Suppress("DEPRECATION")
            settings.allowUniversalAccessFromFileURLs = true
            settings.mediaPlaybackRequiresUserGesture = false
            addJavascriptInterface(JsBridge(bridge), "AndroidBridge")
            webViewClient = WebViewClient()
            webChromeClient = WebChromeClient()
            loadUrl("file:///android_asset/live2d/index.html")
        }
    }
}
