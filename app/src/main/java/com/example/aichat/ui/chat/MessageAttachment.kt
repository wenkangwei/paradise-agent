package com.example.aichat.ui.chat

import androidx.compose.foundation.background
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyHorizontalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AudioFile
import androidx.compose.material.icons.filled.Description
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material.icons.filled.PictureAsPdf
import androidx.compose.material.icons.filled.PlayCircle
import androidx.compose.material.icons.filled.VideoFile
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.example.aichat.ui.chat.model.Attachment

/**
 * Renders attachments inside a message bubble, split into two visual groups:
 *
 *   - Images / videos → 2-column grid of equal-size square thumbnails (max 2 rows)
 *   - Other files     → vertical list of [FileAttachmentCard] rows showing
 *                       icon + filename, so the user always knows what's there
 *
 * Previously the whole attachment list was rendered as a single image Row,
 * which left non-image files as blank boxes. This split fixes that and gives
 * each attachment type a recognizable affordance.
 */
@Composable
fun MessageAttachmentList(
    attachments: List<Attachment>,
    modifier: Modifier = Modifier
) {
    if (attachments.isEmpty()) return

    val images = attachments.filter { it.mimeType.startsWith("image/") }
    val files = attachments.filterNot { it.mimeType.startsWith("image/") }

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

@Composable
private fun ImageGrid(images: List<Attachment>) {
    // LazyHorizontalGrid needs a fixed height; show up to 2 rows of 2 columns = 4 thumbs
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
                    .size(32.dp)
                    .clip(RoundedCornerShape(6.dp))
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)),
                contentAlignment = Alignment.Center
            ) {
                Icon(
                    imageVector = iconFor(attachment.mimeType),
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(20.dp)
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
                    text = friendlyTypeLabel(attachment.mimeType),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

private fun iconFor(mime: String): ImageVector = when {
    mime.startsWith("image/") -> Icons.Filled.Image
    mime.startsWith("video/") -> Icons.Filled.VideoFile
    mime.startsWith("audio/") -> Icons.Filled.AudioFile
    mime == "application/pdf" -> Icons.Filled.PictureAsPdf
    mime.startsWith("text/") -> Icons.Filled.Description
    mime.contains("word") || mime.contains("document") -> Icons.Filled.Description
    mime.contains("sheet") || mime.contains("excel") -> Icons.Filled.Description
    mime.contains("presentation") || mime.contains("powerpoint") -> Icons.Filled.Description
    mime.contains("zip") || mime.contains("compressed") || mime.contains("tar") -> Icons.Filled.Folder
    else -> Icons.Filled.InsertDriveFile
}

private fun friendlyTypeLabel(mime: String): String = when {
    mime.startsWith("image/") -> "图片"
    mime.startsWith("video/") -> "视频"
    mime.startsWith("audio/") -> "音频"
    mime == "application/pdf" -> "PDF 文档"
    mime.startsWith("text/") -> "文本"
    mime.contains("word") || mime.contains("document") -> "Word 文档"
    mime.contains("sheet") || mime.contains("excel") -> "Excel 表格"
    mime.contains("presentation") || mime.contains("powerpoint") -> "PPT 幻灯片"
    mime.contains("zip") || mime.contains("compressed") -> "压缩包"
    mime == "application/octet-stream" -> "文件"
    else -> "文件"
}

private fun fallbackName(attachment: Attachment): String {
    val ext = attachment.mimeType.substringAfter('/').takeIf { it.isNotBlank() }?.let { ".$it" }.orEmpty()
    return "附件$ext"
}
