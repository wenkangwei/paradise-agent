package com.example.aichat.data.provider

import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.remote.Attachment
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.flow.Flow

/**
 * Abstracts the streaming call to an LLM backend. Implementations are produced
 * by [LlmProviderFactory] on demand per [ApiProfileEntity].
 *
 * Currently the only built-in implementation handles OpenAI-compatible
 * providers (which covers Default/OpenAI/DeepSeek/Gemini/Ollama/Custom).
 * An Anthropic-native provider can be added later without changing callers.
 */
interface LlmProvider {

    /**
     * Stream a chat completion. Emits a sequence of [StreamEvent]s that the
     * caller collects to assemble the final assistant message.
     *
     * @param messages full conversation history (domain model)
     * @param attachments optional per-message attachment lists for multimodal calls
     * @param model model name; falls back to the profile's stored value if null
     */
    fun stream(
        messages: List<Message>,
        attachments: List<List<Attachment>>? = null,
        model: String? = null
    ): Flow<StreamEvent>
}
