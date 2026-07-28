package com.example.aichat.ui.chat

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Language
import androidx.compose.material.icons.filled.OpenInFull
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.example.aichat.ui.chat.toolcard.HtmlViewport

/**
 * Detects whether an AI reply looks like a complete HTML page rather than a
 * small inline HTML snippet. Complete pages get rendered as an embedded WebView
 * card; snippets continue to go through MarkdownText.
 */
fun looksLikeHtmlPage(content: String): Boolean {
    val trimmed = content.trim()
    if (trimmed.startsWith("<!DOCTYPE", ignoreCase = true)) return true
    if (trimmed.startsWith("<html", ignoreCase = true)) return true
    val lower = trimmed.lowercase()
    // A self-contained page usually has both head and body; guard against
    // one-liner HTML snippets that don't need a full browser render.
    return lower.contains("<head") && lower.contains("<body") && lower.contains("</html>")
}

/**
 * Renders a complete HTML page inside a message bubble as a small WebView card.
 *
 * Security / performance notes:
 *   - JavaScript is disabled by default to avoid arbitrary code execution from
 *     LLM-generated pages. Most static HTML/CSS renders fine without it.
 *   - The WebView loads from a data URI in a sandboxed process when available.
 *   - Height is capped so a huge page doesn't dominate the chat list.
 */
@Composable
fun HtmlCard(
    html: String,
    modifier: Modifier = Modifier,
    onOpenFullScreen: () -> Unit = {}
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    // v4.2.4: inject viewport meta so the page lays out at device width
    // instead of being zoomed out to fit a default 980px CSS viewport.
    // v4.2.12 #4: also injects <base href="https://aichat.local/">.
    val processedHtml = remember(html) { HtmlViewport.ensureMobileViewport(html) }

    Surface(
        shape = RoundedCornerShape(8.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        tonalElevation = 1.dp,
        modifier = modifier.fillMaxWidth()
    ) {
        Column {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 10.dp, vertical = 6.dp)
            ) {
                Icon(
                    imageVector = Icons.Filled.Language,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(18.dp)
                )
                Spacer(Modifier.size(6.dp))
                Text(
                    text = "HTML 页面",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f)
                )
                // v4.2.12 #4: open the page in the existing ToolCardFullScreen
                // sheet — same JS / zoom / theme configuration as the inline
                // preview, just a lot more pixels to work with.
                IconButton(
                    onClick = onOpenFullScreen,
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.OpenInFull,
                        contentDescription = "全屏查看",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(16.dp)
                    )
                }
                IconButton(
                    onClick = {
                        val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        cm.setPrimaryClip(ClipData.newPlainText("html", html))
                    },
                    modifier = Modifier.size(28.dp)
                ) {
                    Icon(
                        imageVector = Icons.Filled.ContentCopy,
                        contentDescription = "复制源码",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.size(16.dp)
                    )
                }
            }

            AndroidView(
                factory = { ctx ->
                    WebView(ctx).apply {
                        webViewClient = WebViewClient()
                        webChromeClient = WebChromeClient()
                        settings.apply {
                            // v4.2.1: match ToolCardFullScreen.HtmlPreview so an
                            // inline HTML card and its fullscreen counterpart
                            // render identically. JS/domStorage on for SPA pages,
                            // wide viewport + overview for proper layout.
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
                            allowFileAccess = false
                            allowContentAccess = false
                        }
                        // v4.2.12 #4: synthetic origin instead of about:blank
                        // so JS gets a real window.location.origin, relative
                        // URLs resolve against a hostname, and localStorage
                        // has a stable scope. <base href> is also injected
                        // by HtmlViewport for the same reason.
                        loadDataWithBaseURL(
                            "https://aichat.local/",
                            processedHtml,
                            "text/html",
                            "UTF-8",
                            null
                        )
                    }
                },
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 200.dp, max = 480.dp)
                    .clip(RoundedCornerShape(bottomStart = 8.dp, bottomEnd = 8.dp))
            )
        }
    }
}
