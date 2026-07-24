package com.example.aichat.data.remote

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.ByteArrayOutputStream

/**
 * Persists attachment bytes to app-internal storage and provides on-demand
 * base64 encoding for LLM API requests.
 *
 * # Why files, not base64 in Room?
 *
 * The original implementation stored `data:<mime>;base64,<...>` directly in
 * [com.example.aichat.domain.model.Attachment.uri], which gets serialized by
 * [com.example.aichat.data.local.AttachmentConverter] into the `messages` table
 * as JSON. A 2 MB photo becomes ~2.7 MB of base64 text, and the whole message
 * row ends up well over Android's CursorWindow per-row soft cap (~2 MB),
 * producing:
 *
 *   - `CursorWindow: Window is full: requested allocation ... bytes, free space ...`
 *   - `IllegalArgumentException: Row too big to fit cursor window`
 *   - And on some OEM Androids, a native crash in CursorWindow.
 *
 * Files on disk bypass this entirely — Room only stores a short path string.
 * Coil happily loads `file://`/absolute paths and downsamples huge images,
 * which also kills the OOM-on-decode crash path.
 *
 * # Backward compatibility
 *
 * Old rows containing `data:...` URLs still render — Coil's DataUriFetcher
 * decodes them — but they'll still trip the CursorWindow cap on read. Users
 * hitting that should clear the conversation or app data once. All **new**
 * messages go through [persist] and are safe regardless of attachment size.
 */
object AttachmentEncoder {

    private const val SUBDIR = "attachments"

    /** Directory where attachment files live. Created lazily on first call. */
    private fun dir(context: Context): File =
        File(context.filesDir, SUBDIR).apply { if (!exists()) mkdirs() }

    /**
     * Copies the bytes at [sourceUri] (typically a `content://` from the
     * picker or camera) into `filesDir/attachments/<uuid>.<ext>` and returns
     * the **absolute file path** of the stored file.
     *
     * The returned path is what should be persisted in
     * [com.example.aichat.domain.model.Attachment.uri] and handed to Coil for
     * display. Coil accepts absolute paths directly.
     *
     * Must be called on [Dispatchers.IO].
     */
    suspend fun persist(context: Context, sourceUri: Uri): String = withContext(Dispatchers.IO) {
        val mime = guessMimeType(context, sourceUri)
        val ext = extensionFor(mime, sourceUri, context)
        val target = File(dir(context), "${java.util.UUID.randomUUID()}.$ext")
        context.contentResolver.openInputStream(sourceUri).use { input ->
            requireNotNull(input) { "Cannot open stream for URI: $sourceUri" }
            target.outputStream().use { output ->
                input.copyTo(output)
            }
        }
        target.absolutePath
    }

    /**
     * Reads a previously persisted attachment file (by absolute path) and
     * returns it as a `data:<mime>;base64,<...>` URL suitable for the OpenAI
     * `image_url` field in chat completion requests.
     *
     * This is called at send time — the base64 string is ephemeral, lives
     * only in the HTTP request body, and is never persisted to Room.
     *
     * If [filePath] is already a `data:` URL (legacy rows), it is returned
     * as-is so old data continues to work.
     */
    suspend fun toDataUrl(filePath: String): String = withContext(Dispatchers.IO) {
        if (filePath.startsWith("data:") || filePath.startsWith("http")) {
            return@withContext filePath
        }
        val file = File(filePath)
        val bytes = file.readBytes()
        val mime = guessMimeFromExtension(file.name)
        val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
        "data:$mime;base64,$base64"
    }

    /**
     * Best-effort display name extraction. Tries the content provider's
     * DISPLAY_NAME column, falls back to the file name, then to the last URI
     * segment.
     */
    suspend fun displayName(context: Context, uriOrPath: String): String = withContext(Dispatchers.IO) {
        when {
            uriOrPath.startsWith("content://") -> {
                runCatching {
                    context.contentResolver.query(Uri.parse(uriOrPath), arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                        ?.use { c ->
                            if (c.moveToFirst()) c.getString(0) ?: ""
                            else ""
                        } ?: ""
                }.getOrDefault("") ?: ""
            }
            uriOrPath.startsWith("/") -> File(uriOrPath).name
            else -> Uri.parse(uriOrPath).lastPathSegment ?: ""
        }
    }

    // ----- helpers --------------------------------------------------------

    private fun guessMimeType(context: Context, uri: Uri): String {
        val fromType = context.contentResolver.getType(uri)
        if (fromType != null) return fromType
        val name = queryDisplayName(context, uri)
        return mimeFromExtension(name.substringAfterLast('.', "").lowercase())
    }

    private fun guessMimeFromExtension(fileName: String): String {
        val ext = fileName.substringAfterLast('.', "").lowercase()
        return mimeFromExtension(ext)
    }

    private fun mimeFromExtension(ext: String): String = when (ext) {
        "png" -> "image/png"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "bmp" -> "image/bmp"
        "pdf" -> "application/pdf"
        "txt", "md" -> "text/plain"
        "mp4", "m4a" -> "video/mp4"
        "mp3" -> "audio/mpeg"
        "wav" -> "audio/wav"
        "ogg" -> "audio/ogg"
        "json" -> "application/json"
        "csv" -> "text/csv"
        "zip" -> "application/zip"
        else -> "image/jpeg"
    }

    private fun extensionFor(mime: String, uri: Uri, context: Context): String {
        // Prefer the original filename's extension when available — preserves
        // e.g. ".pdf" / ".docx" for non-image uploads.
        val displayName = queryDisplayName(context, uri)
        val fromName = displayName.substringAfterLast('.', "").lowercase()
        if (fromName.isNotEmpty()) return fromName
        return when (mime) {
            "image/png" -> "png"
            "image/gif" -> "gif"
            "image/webp" -> "webp"
            "image/bmp" -> "bmp"
            "application/pdf" -> "pdf"
            "text/plain" -> "txt"
            "video/mp4" -> "mp4"
            "audio/mpeg" -> "mp3"
            "audio/wav" -> "wav"
            else -> "bin"
        }
    }

    private fun queryDisplayName(context: Context, uri: Uri): String {
        val projection = arrayOf(OpenableColumns.DISPLAY_NAME)
        context.contentResolver.query(uri, projection, null, null, null)?.use { cursor ->
            val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (nameIndex >= 0 && cursor.moveToFirst()) {
                return cursor.getString(nameIndex) ?: ""
            }
        }
        return uri.lastPathSegment ?: ""
    }
}
