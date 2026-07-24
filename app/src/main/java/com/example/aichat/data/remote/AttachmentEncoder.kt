package com.example.aichat.data.remote

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream

/**
 * Encodes content:// URIs into base64 data URLs suitable for the OpenAI image_url format.
 *
 * Usage: `val dataUrl = AttachmentEncoder.toDataUrl(context, uri)`
 */
object AttachmentEncoder {

    /**
     * Reads the content at [uri] and returns a base64 data URL string
     * (e.g., "data:image/jpeg;base64,/9j/4AAQ...").
     *
     * Must be called from a background thread (uses [Dispatchers.IO]).
     */
    suspend fun toDataUrl(context: Context, uri: Uri): String = withContext(Dispatchers.IO) {
        val mimeType = guessMimeType(context, uri)
        val bytes = readBytes(context, uri)
        val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
        "data:$mimeType;base64,$base64"
    }

    private fun guessMimeType(context: Context, uri: Uri): String {
        val cr = context.contentResolver
        val fromType = cr.getType(uri)
        if (fromType != null) return fromType

        // Fall back to extension-based guessing
        val name = queryDisplayName(context, uri)
        val ext = name.substringAfterLast('.', "").lowercase()
        return when (ext) {
            "png" -> "image/png"
            "gif" -> "image/gif"
            "webp" -> "image/webp"
            "bmp" -> "image/bmp"
            else -> "image/jpeg"
        }
    }

    private fun readBytes(context: Context, uri: Uri): ByteArray {
        val cr = context.contentResolver
        val inputStream = cr.openInputStream(uri)
            ?: throw IllegalStateException("Cannot open stream for URI: $uri")
        inputStream.use { stream ->
            val buffer = ByteArrayOutputStream()
            val buf = ByteArray(8192)
            while (true) {
                val n = stream.read(buf)
                if (n == -1) break
                buffer.write(buf, 0, n)
            }
            return buffer.toByteArray()
        }
    }

    private fun queryDisplayName(context: Context, uri: Uri): String {
        val projection = arrayOf(OpenableColumns.DISPLAY_NAME)
        context.contentResolver.query(uri, projection, null, null, null)?.use { cursor ->
            val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (nameIndex >= 0 && cursor.moveToFirst()) {
                return cursor.getString(nameIndex)
            }
        }
        return uri.lastPathSegment ?: ""
    }
}
