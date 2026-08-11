package com.example.aichat.domain.usecase

import com.example.aichat.data.provider.StreamEvent
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.di.IoDispatcher
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.withContext
import javax.inject.Inject

/**
 * Lightweight single-shot chat use case for the Live2D Interact tab.
 *
 * Unlike [StreamAiReplyUseCase] (which writes Room rows, spawns a foreground
 * service, and persists every 150ms), this one just collects the stream into
 * a string. The caller can hook [onDelta] for incremental UI updates (typewriter
 * effect on the latest AI turn).
 *
 * No Room, no service, no attachment handling — text in, text out, deltas
 * along the way.
 */
class SingleShotChatUseCase @Inject constructor(
    private val remoteDataSource: ChatRemoteDataSource,
    @IoDispatcher private val io: CoroutineDispatcher,
) {
    /**
     * @param messages conversation history (caller is responsible for trimming)
     * @param onDelta invoked on the IO dispatcher for each [StreamEvent.ContentDelta]
     * @return final assembled assistant text (may be shorter than the sum of
     *   deltas if the stream was cancelled mid-flight; partial result returned)
     */
    suspend fun askStream(
        messages: List<Message>,
        onDelta: (String) -> Unit = {},
    ): String = withContext(io) {
        val sb = StringBuilder()
        try {
            remoteDataSource.streamChat(messages).collect { ev ->
                when (ev) {
                    is StreamEvent.ContentDelta -> {
                        sb.append(ev.text)
                        onDelta(ev.text)
                    }
                    is StreamEvent.Finish -> { /* normal end */ }
                    is StreamEvent.Cancelled -> { /* fall through */ }
                    else -> Unit  // ignore reasoning / tool events for MVP
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            // Swallow — return whatever we accumulated. Caller can surface
            // an error based on empty result if desired.
        }
        sb.toString()
    }
}
