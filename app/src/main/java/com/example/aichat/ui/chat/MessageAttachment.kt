package com.example.aichat.ui.chat

import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyHorizontalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AudioFile
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.GridOn
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PictureAsPdf
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.VideoFile
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.example.aichat.ui.chat.model.Attachment

/**
 * Renders attachments inside a message bubble, split into two visual groups:
 *
 *   - Images / videos → 2-column grid of equal-size square thumbnails (max 2 rows)
 *   - Other files     → vertical list of [FileAttachmentCard] rows showing
 *                       a WPS-style colored icon + filename, so the user
 *                       always knows what's there
 */
@Composable
fun MessageAttachmentList(
    attachments: List<Attachment>,
    modifier: Modifier = Modifier
) {
    if (attachments.isEmpty()) return

    val images = attachments.filter { it.isImage }
    val files = attachments.filterNot { it.isImage }

    Column(modifier = modifier.fillMaxWidth()) {
        if (images.isNotEmpty()) {
            ImageGrid(images)
            if (files.isNotEmpty()) Spacer(Modifier.height(6.dp))
        }
        if (files.isNotEmpty()) {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                files.forEach { FileAttachmentCard(it) }
            }
        }
    }
}

/** Image classification by MIME type and extension fallback. */
private val Attachment.isImage: Boolean
    get() = mimeType.startsWith("image/") || extension() in IMAGE_EXTENSIONS

private val IMAGE_EXTENSIONS = setOf("png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "heif", "svg")

@Composable
private fun ImageGrid(images: List<Attachment>) {
    val rows = if (images.size <= 2) 1 else 2
    val cellMinSize = 120.dp
    LazyHorizontalGrid(
        rows = GridCells.Fixed(rows),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
        modifier = Modifier
            .fillMaxWidth()
            .height(cellMinSize * rows + 4.dp * (rows - 1))
    ) {
        items(
            items = images.take(rows * 3),
            key = { it.id }
        ) { attachment ->
            AsyncImage(
                model = attachment.uri,
                contentDescription = attachment.displayName.ifBlank { "Image" },
                modifier = Modifier
                    .size(cellMinSize)
                    .clip(RoundedCornerShape(8.dp)),
                contentScale = ContentScale.Crop
            )
        }
    }
}

@Composable
private fun FileAttachmentCard(attachment: Attachment) {
    val appearance = fileAppearance(attachment)
    val isAudio = attachment.mimeType.startsWith("audio/") ||
        attachment.extension() in setOf("mp3", "flac", "wav", "aac", "ogg", "m4a")

    // Inline audio player state
    var audioPlaying by remember { mutableStateOf(false) }
    val mediaPlayer = remember { mutableStateOf<android.media.MediaPlayer?>(null) }
    DisposableEffect(Unit) { onDispose { runCatching { mediaPlayer.value?.release() } } }

    Surface(
        shape = RoundedCornerShape(8.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        tonalElevation = 1.dp,
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(appearance.tintColor.copy(alpha = 0.12f)),
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    imageVector = appearance.icon,
                    contentDescription = null,
                    tint = appearance.tintColor,
                    modifier = Modifier.size(22.dp)
                )
            }
            Spacer(Modifier.width(10.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = attachment.displayName.ifBlank { fallbackName(attachment) },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = appearance.label,
                    style = MaterialTheme.typography.labelSmall,
                    color = appearance.tintColor
                )
            }
            if (isAudio) {
                Spacer(Modifier.width(8.dp))
                Icon(
                    imageVector = if (audioPlaying) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                    contentDescription = if (audioPlaying) "暂停" else "播放",
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier
                        .size(32.dp)
                        .clickable {
                            val mp = mediaPlayer.value
                            if (mp != null && mp.isPlaying) {
                                mp.pause()
                                audioPlaying = false
                            } else if (mp != null) {
                                mp.start()
                                audioPlaying = true
                            } else {
                                runCatching {
                                    val newMp = android.media.MediaPlayer().apply {
                                        setDataSource(attachment.uri)
                                        setOnCompletionListener { audioPlaying = false }
                                        prepare()
                                        start()
                                    }
                                    mediaPlayer.value = newMp
                                    audioPlaying = true
                                }
                            }
                        }
                )
            }
        }
    }
}

private data class FileAppearance(
    val icon: ImageVector,
    val tintColor: Color,
    val label: String
)

private fun fileAppearance(attachment: Attachment): FileAppearance {
    val ext = attachment.extension()
    val mime = attachment.mimeType.lowercase()

    return when {
        // Images (only for non-image attachments that slipped through, e.g. image/* saved as octet-stream)
        ext in IMAGE_EXTENSIONS || mime.startsWith("image/") ->
            FileAppearance(Icons.Filled.Image, Color(0xFFE91E63), "图片")

        // PDF
        mime == "application/pdf" || ext == "pdf" ->
            FileAppearance(Icons.Filled.PictureAsPdf, Color(0xFFF44336), "PDF 文档")

        // Word
        ext in setOf("doc", "docx") || mime.contains("word") ->
            FileAppearance(Icons.Filled.Description, Color(0xFF2196F3), "Word 文档")

        // Excel / CSV
        ext in setOf("xls", "xlsx", "csv") ||
            mime.contains("excel") || mime.contains("sheet") ||
            mime == "text/csv" ->
            FileAppearance(Icons.Filled.GridOn, Color(0xFF4CAF50), "表格")

        // PPT
        ext in setOf("ppt", "pptx") ||
            mime.contains("presentation") || mime.contains("powerpoint") ->
            FileAppearance(Icons.Filled.Description, Color(0xFFFF9800), "PPT 幻灯片")

        // Text
        ext in setOf("txt", "md", "log", "json", "xml", "yaml", "yml", "cfg", "ini") ||
            (mime.startsWith("text/") && ext != "csv") ->
            FileAppearance(Icons.Filled.Description, Color(0xFF607D8B), "文本文件")

        // Archive
        ext in setOf("zip", "rar", "7z", "tar", "gz", "bz2") ||
            mime.contains("zip") || mime.contains("compressed") ->
            FileAppearance(Icons.Filled.Folder, Color(0xFF795548), "压缩包")

        // Audio
        mime.startsWith("audio/") || ext in setOf("mp3", "flac", "wav", "aac", "ogg", "m4a") ->
            FileAppearance(Icons.Filled.AudioFile, Color(0xFF00BCD4), "音频")

        // Video
        mime.startsWith("video/") || ext in setOf("mp4", "mkv", "webm", "mov", "m4v") ->
            FileAppearance(Icons.Filled.VideoFile, Color(0xFF9C27B0), "视频")

        // Default
        else -> FileAppearance(Icons.Filled.InsertDriveFile, Color(0xFF9E9E9E), "文件")
    }
}

/** Extract extension from filename first, then URI last segment. */
private fun Attachment.extension(): String {
    val name = displayName.ifBlank { uri.substringAfterLast('/').substringBefore('?') }
    return name.substringAfterLast('.', "").lowercase()
}

private fun fallbackName(attachment: Attachment): String {
    val ext = attachment.extension().let { if (it.isNotBlank()) ".$it" else "" }
    return "附件$ext"
}
