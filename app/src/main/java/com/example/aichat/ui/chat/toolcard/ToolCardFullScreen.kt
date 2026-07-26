package com.example.aichat.ui.chat.toolcard

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * Full-screen sheet for inspecting / collecting / sharing a tool card.
 *
 * Two view modes:
 *   - **Preview**: HTML renders in a WebView (HTML type) or the rendered
 *     Markdown (Markdown type — currently rendered as monospace source since
 *     we have no commonmark dep; future work).
 *   - **Code**: raw source with syntax highlighting (HTML/XML) or plain
 *     monospace (Markdown), plus a "copy all" button in the header.
 *
 * The ⭐ button in the header is delegated to the caller's [onCollect] —
 * the dialog naming + persistence lives in [FavoriteToolDialog].
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
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var showCode by remember { mutableStateOf(false) }
    val context = LocalContext.current

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        modifier = Modifier.fillMaxSize()
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = {
                        Text(
                            text = title,
                            style = MaterialTheme.typography.titleMedium,
                            maxLines = 1
                        )
                    },
                    navigationIcon = {
                        IconButton(onClick = onDismiss) {
                            Icon(Icons.Filled.Close, contentDescription = "关闭")
                        }
                    },
                    actions = {
                        // Code / Preview toggle
                        ViewModeChip(showCode = showCode, onChange = { showCode = it })
                        IconButton(onClick = onCollect) {
                            Icon(Icons.Filled.Star, contentDescription = "收藏")
                        }
                        IconButton(
                            onClick = {
                                copyToClipboard(context, content, "tool_source")
                            }
                        ) {
                            Icon(Icons.Filled.ContentCopy, contentDescription = "复制全部")
                        }
                        IconButton(onClick = onShare) {
                            Icon(
                                imageVector = Icons.Filled.Share,
                                contentDescription = "分享"
                            )
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

@Composable
private fun ViewModeChip(showCode: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier
            .clip(CircleShape)
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f)),
        verticalAlignment = Alignment.CenterVertically
    ) {
        FilterChip(
            selected = !showCode,
            onClick = { onChange(false) },
            label = { Text("预览", style = MaterialTheme.typography.labelMedium) }
        )
        Spacer(Modifier.size(4.dp))
        FilterChip(
            selected = showCode,
            onClick = { onChange(true) },
            label = { Text("代码", style = MaterialTheme.typography.labelMedium) }
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

@Composable
private fun HtmlPreview(content: String) {
    // Reuse the same WebView config as HtmlCard.kt (JS off, zoom on).
    AndroidView(
        factory = { ctx ->
            WebView(ctx).apply {
                webViewClient = WebViewClient()
                webChromeClient = WebChromeClient()
                settings.apply {
                    javaScriptEnabled = false
                    domStorageEnabled = false
                    cacheMode = WebSettings.LOAD_NO_CACHE
                    setSupportZoom(true)
                    builtInZoomControls = true
                    displayZoomControls = false
                }
                loadDataWithBaseURL(null, content, "text/html", "UTF-8", null)
            }
        },
        modifier = Modifier.fillMaxSize()
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
