package com.example.aichat.ui.chat.toolcard

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.example.aichat.data.repository.FavoriteToolType
import java.io.File
import java.util.UUID

/**
 * Shares a tool card as a standalone file via [Intent.ACTION_SEND].
 *
 * HTML cards are written verbatim — any browser opens them directly.
 * Markdown cards are written as `.md` files — the receiver needs a Markdown
 * viewer (most chat apps and editors handle this).
 *
 * WHY not convert Markdown → HTML on share:
 *   commonmark-java adds ~300 KB; the existing MarkdownText.kt parser is
 *   hand-rolled and Compose-only (it doesn't emit an HTML string). For now
 *   we ship raw Markdown and let the receiver render. If we later add a
 *   Markdown-to-HTML lib, swap the file content here without touching the
 *   call sites.
 *
 * The file is written to the app's [Context.getCacheDir] under `shared/`,
 * served through a FileProvider authority declared in AndroidManifest.xml.
 */
object ToolSharer {

    private const val SUBDIR = "shared"

    /**
     * Builds and launches a share intent. Returns the filename written (for
     * debugging / future "open shared location" features). Throws if the
     * FileProvider is misconfigured — callers should wrap in try/catch.
     */
    fun share(
        context: Context,
        title: String,
        content: String,
        type: ToolType,
        authority: String = "${context.packageName}.fileprovider"
    ): String {
        val (file, mime) = writeToolFile(context, title, content, type)
        val uri = FileProvider.getUriForFile(context, authority, file)

        val shareIntent = Intent(Intent.ACTION_SEND).apply {
            this.type = mime
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, title)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        // Use chooser so the user can pick WeChat / browser / files app.
        val chooser = Intent.createChooser(shareIntent, "分享工具").apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(chooser)
        return file.name
    }

    /**
     * Same as [share] but accepts the repository-layer enum so callers that
     * hold [FavoriteToolType] (e.g. drawer reuse) don't have to map it.
     */
    fun share(
        context: Context,
        title: String,
        content: String,
        type: FavoriteToolType,
        authority: String = "${context.packageName}.fileprovider"
    ): String = share(context, title, content, type.toToolType(), authority)

    private fun writeToolFile(
        context: Context,
        title: String,
        content: String,
        type: ToolType
    ): Pair<File, String> {
        val dir = File(context.cacheDir, SUBDIR).apply { if (!exists()) mkdirs() }
        val ext = when (type) {
            ToolType.HTML -> "html"
            ToolType.MARKDOWN -> "md"
        }
        val mime = when (type) {
            ToolType.HTML -> "text/html"
            ToolType.MARKDOWN -> "text/markdown"
        }
        val safeTitle = sanitizeFilename(title).ifBlank { "tool" }
        val file = File(dir, "${safeTitle}_${UUID.randomUUID().toString().take(8)}.$ext")
        file.writeText(content)
        return file to mime
    }

    private fun sanitizeFilename(name: String): String {
        // Strip path separators and trim — a long HTML <title> may contain
        // characters that aren't filesystem-friendly on all Android variants.
        return name.lineSequence().firstOrNull().orEmpty()
            .replace(Regex("[\\\\/:*?\"<>|]"), "")
            .replace(' ', '_')
            .take(40)
            .trim()
    }

    private fun FavoriteToolType.toToolType(): ToolType = when (this) {
        FavoriteToolType.HTML -> ToolType.HTML
        FavoriteToolType.MARKDOWN -> ToolType.MARKDOWN
    }
}
