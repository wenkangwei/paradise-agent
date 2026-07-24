package com.example.aichat.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.example.aichat.ui.chat.model.Attachment

/**
 * Single attachment thumbnail shown above the input bar.
 * Displays the image at 80dp with a remove button overlay.
 */
@Composable
fun AttachmentPreview(
    attachment: Attachment,
    onRemove: () -> Unit,
    modifier: Modifier = Modifier
) {
    Box(
        modifier = modifier.size(80.dp)
    ) {
        AsyncImage(
            model = attachment.uri,
            contentDescription = attachment.displayName.ifBlank { "Attachment" },
            modifier = Modifier
                .size(80.dp)
                .clip(RoundedCornerShape(8.dp)),
            contentScale = ContentScale.Crop
        )

        // Remove button overlay (top-right corner)
        IconButton(
            onClick = onRemove,
            modifier = Modifier
                .align(Alignment.TopEnd)
                .size(24.dp)
                .clip(RoundedCornerShape(50))
                .background(
                    MaterialTheme.colorScheme.surface.copy(alpha = 0.8f)
                )
        ) {
            Icon(
                imageVector = Icons.Filled.Close,
                contentDescription = "Remove attachment",
                tint = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.size(16.dp)
            )
        }
    }
}
