package com.example.aichat.ui.chat

import android.content.Context
import android.net.Uri
import androidx.core.content.FileProvider
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Creates a writable [Uri] backed by a cache file, suitable for passing to
 * [androidx.activity.result.contract.ActivityResultContracts.TakePicture].
 *
 * The URI is scoped to the app's cache/captures directory and exposed via
 * FileProvider under authority `<package>.fileprovider` (see AndroidManifest
 * and res/xml/file_paths.xml).
 *
 * The returned File is kept around so we can read it after capture completes.
 * Cleanup is the caller's responsibility (the file lives in cache/, which the
 * system can evict under disk pressure).
 */
object CaptureUriProvider {

    fun newImageUri(context: Context): Uri {
        val dir = File(context.cacheDir, "captures").apply { if (!exists()) mkdirs() }
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val file = File(dir, "CAP_$timestamp.jpg")
        return FileProvider.getUriForFile(
            context,
            "${context.packageName}.fileprovider",
            file
        )
    }
}
