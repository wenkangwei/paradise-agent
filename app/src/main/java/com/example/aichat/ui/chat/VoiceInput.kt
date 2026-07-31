package com.example.aichat.ui.chat

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import org.vosk.Model
import org.vosk.Recognizer
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream

data class VoiceRecorder(
    val start: () -> Unit,
    val stop: () -> Unit,
    val isRecording: State<Boolean>,
    val rmsLevel: State<Float>,
    val isAvailable: Boolean
)

data class VoiceResult(
    val wavFile: File,
    val transcript: String
)

@Composable
fun rememberVoiceRecorder(
    context: Context = LocalContext.current,
    onResult: (VoiceResult) -> Unit
): VoiceRecorder {
    val isRecording = remember { mutableStateOf(false) }
    val rms = remember { mutableStateOf(0f) }
    val shouldStop = remember { mutableStateOf(false) }
    var currentThread: Thread? = null

    DisposableEffect(Unit) {
        onDispose {
            shouldStop.value = true
            currentThread?.interrupt()
        }
    }

    fun startRecording() {
        shouldStop.value = false
        isRecording.value = true
        rms.value = 0f

        currentThread = Thread {
            var audioRecord: AudioRecord? = null
            val wavFile = File(context.cacheDir, "voice_${System.currentTimeMillis()}.wav")
            val rawFile = File(context.cacheDir, "voice_${System.currentTimeMillis()}.pcm")

            try {
                val sampleRate = 16000
                val bufferSize = AudioRecord.getMinBufferSize(
                    sampleRate,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT
                )

                audioRecord = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    sampleRate,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize * 2
                )

                // Write PCM in little-endian (WAV/Vosk native format)
                val pcmBuf = java.nio.ByteBuffer.allocate(bufferSize * 2)
                    .order(java.nio.ByteOrder.LITTLE_ENDIAN)
                val pcmOut = rawFile.outputStream()
                val buffer = ShortArray(bufferSize)

                audioRecord.startRecording()

                while (!shouldStop.value) {
                    val read = audioRecord.read(buffer, 0, buffer.size)
                    if (read > 0) {
                        var sum = 0L
                        for (i in 0 until read) sum += (buffer[i] * buffer[i]).toLong()
                        rms.value = (kotlin.math.sqrt(sum.toDouble() / read) / 32768.0)
                            .toFloat().coerceIn(0f, 1f)

                        pcmBuf.clear()
                        for (i in 0 until read) pcmBuf.putShort(buffer[i])
                        pcmOut.write(pcmBuf.array(), 0, read * 2)
                    }
                }

                audioRecord.stop()
                pcmOut.close()

                // Convert PCM to WAV
                pcmToWav(rawFile, wavFile, sampleRate)
                rawFile.delete()

                isRecording.value = false
                rms.value = 0f

                // Try Vosk transcription
                val text = voskTranscribe(context, wavFile)
                onResult(VoiceResult(wavFile, text))

            } catch (e: Exception) {
                e.printStackTrace()
                isRecording.value = false
                rms.value = 0f
                try { onResult(VoiceResult(wavFile, "")) } catch (_: Exception) {}
            } finally {
                runCatching { audioRecord?.release() }
            }
        }.apply { start() }
    }

    return VoiceRecorder(
        start = { startRecording() },
        stop = { shouldStop.value = true },
        isRecording = isRecording,
        rmsLevel = rms,
        isAvailable = true
    )
}

/** Convert raw PCM to WAV format using ByteBuffer for proper endianness. */
private fun pcmToWav(pcmFile: File, wavFile: File, sampleRate: Int) {
    val pcmData = pcmFile.readBytes()
    val pcmSize = pcmData.size
    val channels = 1
    val bitsPerSample = 16
    val byteRate = sampleRate * channels * bitsPerSample / 8
    val blockAlign = channels * bitsPerSample / 8

    val buf = java.nio.ByteBuffer.allocate(44 + pcmSize)
        .order(java.nio.ByteOrder.LITTLE_ENDIAN)
    buf.put("RIFF".toByteArray())
    buf.putInt(36 + pcmSize)
    buf.put("WAVE".toByteArray())
    buf.put("fmt ".toByteArray())
    buf.putInt(16)           // PCM
    buf.putShort(1)          // format = PCM
    buf.putShort(channels.toShort())
    buf.putInt(sampleRate)
    buf.putInt(byteRate)
    buf.putShort(blockAlign.toShort())
    buf.putShort(bitsPerSample.toShort())
    buf.put("data".toByteArray())
    buf.putInt(pcmSize)
    buf.put(pcmData)
    wavFile.writeBytes(buf.array())
}

/** Transcribe WAV file using Vosk offline recognizer. */
private fun voskTranscribe(context: Context, wavFile: File): String {
    val modelPath = resolveVoskModel(context) ?: return "[Vosk: 模型未找到]"
    try {
        val model = Model(modelPath)
        val recognizer = Recognizer(model, 16000.0f)

        // Read WAV, skip 44-byte header, convert to ShortArray (LE)
        val audioData = wavFile.readBytes()
        var hasAudio = false
        if (audioData.size > 44) {
            val pcmBytes = audioData.copyOfRange(44, audioData.size)
            if (pcmBytes.size >= 3200) {
                hasAudio = true
                val pcmBuf = java.nio.ByteBuffer.wrap(pcmBytes)
                    .order(java.nio.ByteOrder.LITTLE_ENDIAN)
                val shorts = ShortArray(pcmBytes.size / 2)
                pcmBuf.asShortBuffer().get(shorts)
                // Feed in 0.5s chunks
                val chunkSize = 8000
                var offset = 0
                while (offset < shorts.size) {
                    val len = minOf(chunkSize, shorts.size - offset)
                    recognizer.acceptWaveForm(shorts.copyOfRange(offset, offset + len), len)
                    offset += len
                }
            }
        }
        if (!hasAudio) return "[Vosk: 无音频数据]"

        val json = recognizer.finalResult
        recognizer.close()
        model.close()
        val text = extractVoskText(json)
        return text.replace(" ", "").ifEmpty { "[Vosk: 未识别到文字]" }
    } catch (e: Exception) {
        return "[Vosk: ${e.javaClass.simpleName}]"
    }
}

private fun resolveVoskModel(context: Context): String? {
    // Check app files dir first
    val internalDir = File(context.filesDir, "vosk-model-small-cn")
    if (internalDir.exists() && File(internalDir, "am/final.mdl").exists()) {
        return internalDir.absolutePath
    }
    // Check sdcard (user pushed model here)
    val sdcardDir = File("/sdcard/vosk-model")
    if (sdcardDir.exists() && File(sdcardDir, "am/final.mdl").exists()) {
        return sdcardDir.absolutePath
    }
    return null
}

private fun extractVoskText(json: String): String {
    if (json.isEmpty()) return ""
    return try {
        Regex("\"text\"\\s*:\\s*\"(.*?)\"").find(json)?.groupValues?.get(1).orEmpty()
    } catch (_: Exception) { "" }
}
