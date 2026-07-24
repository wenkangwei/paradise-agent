package com.example.aichat.data.provider

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector

/**
 * Identifies the protocol family a supplier speaks. Drives which [LlmProvider]
 * implementation handles requests for an [com.example.aichat.data.local.entity.ApiProfileEntity]
 * bound to this supplier.
 */
enum class SupplierCategory {
    DEFAULT_SERVER,
    OPENAI_COMPATIBLE,
    ANTHROPIC_NATIVE,
    LOCAL
}

/**
 * Optional capability flags advertised by a supplier. Used by the UI to show
 * feature availability and (future) by Agent routing to pick a profile that
 * supports e.g. tool calling or vision.
 */
enum class Capability {
    STREAMING,
    MULTIMODAL,
    TOOL_CALLING,
    REASONING,
    RAG
}

/**
 * Describes an extra form field a supplier needs beyond the standard set.
 * Used by the API config edit form to render supplier-specific inputs.
 */
data class FieldSpec(
    val key: String,
    val label: String,
    val required: Boolean = false,
    val defaultValue: String = ""
)

/**
 * A supplier is a static description of an LLM provider brand/protocol family
 * (e.g. "DeepSeek", "OpenAI", "Ollama"). Concrete connection details live in
 * per-user [com.example.aichat.data.local.entity.ApiProfileEntity] rows.
 *
 * Instances are read-only and registered at app start via [SupplierRegistry].
 */
interface Supplier {
    val id: String
    val displayName: String
    val category: SupplierCategory
    val defaultBaseUrl: String
    val suggestedModels: List<String>
    val apiKeyRequired: Boolean
    val brandColor: Long
    val extraHeaders: Map<String, String> get() = emptyMap()
    val capabilities: Set<Capability>
    val customFields: List<FieldSpec> get() = emptyList()
}
