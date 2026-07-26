package com.example.aichat.ui.chat.toolcard

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.input.pointer.pointerInteropFilter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * Full-screen sheet for inspecting / collecting / sharing a tool card.
 *
 * v4.2.3 redesign (browser-style):
 *   - Renders via [androidx.compose.ui.window.Dialog] with
 *     `usePlatformDefaultWidth = false` instead of [ModalBottomSheet].
 *     ModalBottomSheet's swipe-to-dismiss nestedScroll was eating the
 *     vertical-drag gesture the WebView needs to scroll its own content;
 *     Dialog gives us a clean fullscreen surface with no gesture conflicts.
 *   - TopAppBar drops the title entirely (per spec: "不显示页面标题，
 *     直接把组件都放到一行"). All controls live in a single row:
 *     [← close]  [预览|代码 toggle]  [⭐] [⧉] [↗]  — like a browser
 *     toolbar. The toggle is compact (130dp wide).
 *   - The toggle's indicator AND label colours animate together so the
 *     "selected" state slides instead of teleporting while the pill
 *     catches up (previous bug: text flipped immediately, pill arrived
 *     200ms later → looked broken).
 *   - The HTML WebView gets `pointerInteropFilter { false }` so touch
 *     events are NOT consumed by the Compose gesture system and reach the
 *     WebView directly — this is the documented fix for "WebView can't
 *     scroll inside Compose" on Material3 1.2+.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ToolCardFullScreen(
    title: String,
    content: String,
    type: ToolType,
    onDismiss: () -> Unit,
    onCollect: () -> Unit,
    onShare: () -> Unit,
) {
    var showCode by remember { mutableStateOf(false) }
    val context = LocalContext.current

    // Hand the Android back button to dismiss — Dialog already does this for
    // back-out, but being explicit avoids surprises when the host Activity
    // has its own onBackPressed.
    BackHandler(onBack = onDismiss)

    androidx.compose.ui.window.Dialog(
        onDismissRequest = onDismiss,
        properties = androidx.compose.ui.window.DialogProperties(
            usePlatformDefaultWidth = false,
            dismissOnBackPress = true,
            dismissOnClickOutside = false
        )
    ) {
        Scaffold(
            modifier = Modifier.fillMaxSize(),
            topBar = {
                TopAppBar(
                    title = {},
                    navigationIcon = {
                        IconButton(onClick = onDismiss) {
                            Icon(Icons.Filled.Close, contentDescription = "关闭")
                        }
                    },
                    actions = {
                        if (type == ToolType.HTML) {
                            SlidingSegmentedControl(
                                showCode = showCode,
                                onChange = { showCode = it }
                            )
                            Spacer(Modifier.size(8.dp))
                        }
                        IconButton(onClick = onCollect) {
                            Icon(Icons.Filled.Star, contentDescription = "收藏")
                        }
                        IconButton(
                            onClick = { copyToClipboard(context, content, "tool_source") }
                        ) {
                            Icon(Icons.Filled.ContentCopy, contentDescription = "复制全部")
                        }
                        IconButton(onClick = onShare) {
                            Icon(Icons.Filled.Share, contentDescription = "分享")
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(
                        containerColor = MaterialTheme.colorScheme.surface
                    )
                )
            }
        ) { innerPadding ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
            ) {
                if (showCode) {
                    CodeView(content = content, type = type)
                } else {
                    PreviewView(content = content, type = type)
                }
            }
        }
    }
}

/**
 * iOS-style two-segment toggle with an animated sliding pill indicator.
 *
 * v4.2.3: 130dp wide × 30dp tall (down from 180×34) so it fits alongside
 * three icon buttons in a single TopAppBar action row.
 */
@Composable
private fun SlidingSegmentedControl(
    showCode: Boolean,
    onChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier
) {
    val indicator by animateFloatAsState(
        targetValue = if (showCode) 1f else 0f,
        animationSpec = spring(
            dampingRatio = Spring.DampingRatioMediumBouncy,
            stiffness = Spring.StiffnessMediumLow
        ),
        label = "segment-indicator"
    )
    val primary = MaterialTheme.colorScheme.primary
    val trackColor = MaterialTheme.colorScheme.surfaceVariant

    Surface(
        modifier = modifier
            .width(130.dp)
            .height(30.dp),
        shape = RoundedCornerShape(15.dp),
        color = trackColor
    ) {
        BoxWithConstraints {
            val halfWidthDp = maxWidth / 2
            Box(
                modifier = Modifier
                    .fillMaxHeight()
                    .width(halfWidthDp)
                    .padding(3.dp)
                    .clip(RoundedCornerShape(12.dp))
                    .background(primary)
                    // indicator ∈ [0,1] → x offset ∈ [0, halfWidth]
                    .offset(x = (indicator * halfWidthDp.value).dp, y = 0.dp)
            )
            Row(modifier = Modifier.fillMaxSize()) {
                SegmentedLabel(
                    text = "预览",
                    selected = !showCode,
                    modifier = Modifier.weight(1f),
                    onClick = { onChange(false) }
                )
                SegmentedLabel(
                    text = "代码",
                    selected = showCode,
                    modifier = Modifier.weight(1f),
                    onClick = { onChange(true) }
                )
            }
        }
    }
}

@Composable
private fun SegmentedLabel(
    text: String,
    selected: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    // v4.2.3: animate the label colour so it transitions smoothly WITH the
    // sliding pill instead of teleporting. Spring ties it to the same
    // perceived "speed" as the pill slide.
    val targetColor = if (selected) MaterialTheme.colorScheme.onPrimary
                      else MaterialTheme.colorScheme.onSurfaceVariant
    val color by animateColorAsState(
        targetValue = targetColor,
        animationSpec = spring(
            dampingRatio = Spring.DampingRatioNoBouncy,
            stiffness = Spring.StiffnessMedium
        ),
        label = "segment-label-color"
    )
    val interaction = remember { MutableInteractionSource() }
    Box(
        modifier = modifier
            .fillMaxSize()
            .clickable(
                interactionSource = interaction,
                indication = null,
                onClick = onClick
            ),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.labelMedium,
            color = color
        )
    }
}

@Composable
private fun CodeView(content: String, type: ToolType) {
    val annotated = remember(content, type) {
        if (type == ToolType.HTML) CodeHighlighter.highlight(content, "html")
        else CodeHighlighter.highlight(content, "markdown")
    }
    val scroll = rememberScrollState()
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .padding(16.dp)
            .verticalScroll(scroll)
    ) {
        Text(
            text = annotated,
            style = MaterialTheme.typography.bodySmall.copy(
                fontFamily = FontFamily.Monospace,
                lineHeight = MaterialTheme.typography.bodySmall.fontSize * 1.4
            )
        )
    }
}

@Composable
private fun PreviewView(content: String, type: ToolType) {
    when (type) {
        ToolType.HTML -> HtmlPreview(content = content)
        ToolType.MARKDOWN -> MarkdownPreviewFallback(content = content)
    }
}

@SuppressLint("SetJavaScriptEnabled")
@OptIn(ExperimentalComposeUiApi::class)
@Composable
private fun HtmlPreview(content: String) {
    // Browser-grade WebView config: JS + DOM storage on so React/Vue/alpine
    // pages render correctly; wide viewport + overview mode so the page lays
    // out at the device width instead of forcing 980px default viewport;
    // zoom enabled for pinch-to-zoom on dense tables/SVGs.
    //
    // pointerInteropFilter { false } on the AndroidView modifier is the
    // documented fix for "WebView can't scroll inside Compose" on Material3
    // 1.2+: it tells the Compose gesture system NOT to consume touch events,
    // so they fall through to the WebView directly. Without this, the parent
    // Scaffold/Box swallows vertical drags and the page appears frozen.
    AndroidView(
        factory = { ctx ->
            WebView(ctx).apply {
                webViewClient = WebViewClient()
                webChromeClient = WebChromeClient()
                // Explicit scroll enablement — the WebView should behave like
                // a browser viewport: scrollable, with overscroll glow, and
                // flagged as a scroll container so the framework routes
                // vertical drag gestures here instead of to the parent.
                isVerticalScrollBarEnabled = true
                isHorizontalScrollBarEnabled = true
                isScrollContainer = true
                overScrollMode = View.OVER_SCROLL_ALWAYS
                settings.apply {
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    databaseEnabled = true
                    cacheMode = WebSettings.LOAD_DEFAULT
                    setSupportZoom(true)
                    builtInZoomControls = true
                    displayZoomControls = false
                    useWideViewPort = true
                    loadWithOverviewMode = true
                    defaultTextEncodingName = "UTF-8"
                    mediaPlaybackRequiresUserGesture = true
                    // Allow JS-driven navigation (window.location, target=_blank
                    // etc.) to be intercepted by the WebViewClient rather than
                    // spawning the external browser unexpectedly.
                    setSupportMultipleWindows(false)
                    javaScriptCanOpenWindowsAutomatically = false
                    // Don't allow file:/ content access from LLM-generated
                    // HTML — same-origin policy stays intact.
                    allowFileAccess = false
                    allowContentAccess = false
                }
                // Force the webview to inherit the app's dark/light theme so
                // pages with `color-scheme: light dark` look right.
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
                    settings.forceDark = WebSettings.FORCE_DARK_OFF
                }
                loadDataWithBaseURL("about:blank", content, "text/html", "UTF-8", null)
            }
        },
        modifier = Modifier
            .fillMaxSize()
            .pointerInteropFilter { false }
    )
}

/**
 * Without commonmark-java we can't render Markdown → HTML in the sheet.
 * Fall back to a scrollable monospace view; the user can still read it and
 * the "复制全部" button in the header works. The chat bubble still shows
 * it rendered via MarkdownText (the bubble path uses our hand-rolled
 * parser, but that one renders Compose nodes directly, not AnnotatedString).
 */
@Composable
private fun MarkdownPreviewFallback(content: String) {
    val scroll = rememberScrollState()
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.surface)
            .padding(16.dp)
            .verticalScroll(scroll)
    ) {
        Text(
            text = content,
            style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace)
        )
    }
}

private fun copyToClipboard(context: Context, text: String, label: String) {
    val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText(label, text))
}
