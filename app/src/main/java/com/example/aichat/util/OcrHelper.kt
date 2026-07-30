package com.example.aichat.util

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import java.io.InputStream

/**
 * ML Kit OCR helper — on-device Chinese text recognition.
 *
 * Model (~30MB) auto-downloads on first use, then works offline.
 * No API key or network required after initial download.
 *
 * Usage:
 *   val text = OcrHelper.recognize(context, imageUri)
 */
object OcrHelper {

    private val recognizer by lazy {
        TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
    }

    /**
     * Recognize text from an image URI.
     * @return recognized text string, or empty on failure.
     */
    suspend fun recognize(context: Context, uri: Uri): String = withContext(Dispatchers.IO) {
        try {
            val bitmap: Bitmap = uri.toBitmap(context) ?: return@withContext ""
            val image = InputImage.fromBitmap(bitmap, 0)
            val result = recognizer.process(image).await()
            result.text
        } catch (e: Exception) {
            e.printStackTrace()
            ""
        }
    }

    /**
     * Recognize text from a bitmap directly.
     */
    suspend fun recognize(bitmap: Bitmap): String = withContext(Dispatchers.IO) {
        try {
            val image = InputImage.fromBitmap(bitmap, 0)
            val result = recognizer.process(image).await()
            result.text
        } catch (e: Exception) {
            e.printStackTrace()
            ""
        }
    }

    private fun Uri.toBitmap(context: Context): Bitmap? {
        return try {
            val inputStream: InputStream? = context.contentResolver.openInputStream(this)
            BitmapFactory.decodeStream(inputStream)
        } catch (e: Exception) {
            null
        }
    }
}
