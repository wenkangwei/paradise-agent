package com.example.aichat.data.provider

import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.local.entity.ConversationEntity
import com.example.aichat.data.local.entity.MessageEntity

/**
 * Built-in [Supplier] implementations. One file on purpose: easy to scan and extend.
 *
 * To add a new supplier: append an object below and add it to [BuiltinSuppliers.all].
 * No other code needs to change.
 *
 * IMPORTANT: Every baseUrl MUST end with `/` and include the API version segment
 * (e.g. `/v1/`, `/v4/`). The AiApiService uses relative path `chat/completions`,
 * so the version segment is part of the baseUrl by contract.
 */

object DefaultServerSupplier : Supplier {
    override val id = "default"
    override val displayName = "默认服务器"
    override val category = SupplierCategory.DEFAULT_SERVER
    override val defaultBaseUrl = "https://api.wehost.com/v1/"
    override val suggestedModels = listOf("wehost-chat", "wehost-r1")
    override val apiKeyRequired = true
    override val brandColor = 0xFF00696D
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.REASONING)
}

object OpenAiSupplier : Supplier {
    override val id = "openai"
    override val displayName = "OpenAI"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.openai.com/v1/"
    override val suggestedModels = listOf("gpt-4o-mini", "gpt-4o", "o1-mini", "gpt-4-turbo")
    override val apiKeyRequired = true
    override val brandColor = 0xFF10A37F
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING, Capability.REASONING)
}

object DeepSeekSupplier : Supplier {
    override val id = "deepseek"
    override val displayName = "DeepSeek (深度求索)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.deepseek.com/v1/"
    override val suggestedModels = listOf("deepseek-chat", "deepseek-reasoner")
    override val apiKeyRequired = true
    override val brandColor = 0xFF4D6BFE
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.REASONING)
}

object AnthropicSupplier : Supplier {
    override val id = "anthropic"
    override val displayName = "Anthropic Claude"
    override val category = SupplierCategory.ANTHROPIC_NATIVE
    override val defaultBaseUrl = "https://api.anthropic.com/v1/"
    override val suggestedModels = listOf("claude-3-7-sonnet-20250219", "claude-3-5-haiku-20241022")
    override val apiKeyRequired = true
    override val brandColor = 0xFFD97757
    override val extraHeaders = mapOf("anthropic-version" to "2023-06-01")
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING, Capability.REASONING)
}

object GeminiSupplier : Supplier {
    override val id = "gemini"
    override val displayName = "Google Gemini"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://generativelanguage.googleapis.com/v1beta/openai/"
    override val suggestedModels = listOf("gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash")
    override val apiKeyRequired = true
    override val brandColor = 0xFF4285F4
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING)
}

object OllamaSupplier : Supplier {
    override val id = "ollama"
    override val displayName = "Ollama (本地)"
    override val category = SupplierCategory.LOCAL
    override val defaultBaseUrl = "http://10.0.2.2:11434/v1/"  // 10.0.2.2 = 宿主机 in Android emulator
    override val suggestedModels = listOf("qwen2.5:7b", "llama3.2:3b", "deepseek-r1:7b")
    override val apiKeyRequired = false  // 本地服务无需 key，但 API 要求非空，UI 可填任意字符串
    override val brandColor = 0xFF000000
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL)
}

/** 智谱 GLM (open.bigmodel.cn) — includes GLM-4 + GLM Coding Plan models */
object ZhipuGlmSupplier : Supplier {
    override val id = "zhipu_glm"
    override val displayName = "智谱 GLM (bigmodel)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://open.bigmodel.cn/api/paas/v4/"
    override val suggestedModels = listOf(
        "glm-4-plus",
        "glm-4-air",
        "glm-4-airx",
        "glm-4-long",
        "glm-4-flash",
        "glm-4-flashx",
        "glm-4-0520",
        "glm-4"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF3366FF
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING, Capability.REASONING)
}

/** 阿里通义千问 (DashScope OpenAI-compatible endpoint) */
object QwenSupplier : Supplier {
    override val id = "qwen"
    override val displayName = "阿里通义千问 (DashScope)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1/"
    override val suggestedModels = listOf(
        "qwen-max",
        "qwen-plus",
        "qwen-turbo",
        "qwen-long",
        "qwen2.5-72b-instruct",
        "qwen2.5-32b-instruct",
        "qwen2.5-7b-instruct",
        "qwq-32b-preview",
        "qwen-vl-max",
        "qwen-vl-plus"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF615CED
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING, Capability.REASONING)
}

/** 月之暗面 Kimi */
object KimiSupplier : Supplier {
    override val id = "kimi"
    override val displayName = "Moonshot Kimi (月之暗面)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.moonshot.cn/v1/"
    override val suggestedModels = listOf(
        "moonshot-v1-8k",
        "moonshot-v1-32k",
        "moonshot-v1-128k",
        "kimi-latest"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF1F1F1F
    override val capabilities = setOf(Capability.STREAMING, Capability.TOOL_CALLING, Capability.REASONING)
}

/** 字节豆包 */
object DoubaoSupplier : Supplier {
    override val id = "doubao"
    override val displayName = "火山引擎豆包 (Volcengine)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    // 火山引擎 Ark OpenAI 兼容端点
    override val defaultBaseUrl = "https://ark.cn-beijing.volces.com/api/v3/"
    override val suggestedModels = listOf(
        "doubao-pro-32k",
        "doubao-pro-128k",
        "doubao-lite-32k",
        "doubao-lite-128k",
        "doubao-1.5-pro-32k",
        "doubao-1.5-lite-32k"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF0FCDA5
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING)
}

/** 百度千帆 (ERNIE Bot OpenAI-compatible) */
object QianfanSupplier : Supplier {
    override val id = "qianfan"
    override val displayName = "百度千帆 (ERNIE)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://qianfan.baidubce.com/v2/"
    override val suggestedModels = listOf(
        "ernie-4.0-8k-latest",
        "ernie-4.0-turbo-8k",
        "ernie-3.5-128k",
        "ernie-speed-128k",
        "ernie-lite-8k",
        "deepseek-v3"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF2932E1
    override val capabilities = setOf(Capability.STREAMING, Capability.TOOL_CALLING, Capability.REASONING)
}

/** 讯飞星火 — OpenAI 兼容端点 */
object SparkSupplier : Supplier {
    override val id = "spark"
    override val displayName = "讯飞星火 (Spark)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://spark-api-open.xf-yun.com/v1/"
    override val suggestedModels = listOf(
        "4.0Ultra",
        "generalv3.5",
        "generalv3",
        "lite"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF0085D0
    override val capabilities = setOf(Capability.STREAMING, Capability.TOOL_CALLING)
}

/** 零一万物 (01.AI) */
object LingyiSupplier : Supplier {
    override val id = "lingyi"
    override val displayName = "零一万物 (01.AI)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.lingyiwanwu.com/v1/"
    override val suggestedModels = listOf("yi-large", "yi-medium", "yi-lightning", "yi-vision")
    override val apiKeyRequired = true
    override val brandColor = 0xFF003D2D
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL)
}

/** 腾讯混元 */
object HunyuanSupplier : Supplier {
    override val id = "hunyuan"
    override val displayName = "腾讯混元 (Hunyuan)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.hunyuan.cloud.tencent.com/v1/"
    override val suggestedModels = listOf(
        "hunyuan-pro",
        "hunyuan-standard",
        "hunyuan-lite",
        "hunyuan-large",
        "hunyuan-large-longcontext"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFF0053E0
    override val capabilities = setOf(Capability.STREAMING, Capability.TOOL_CALLING)
}

/** SiliconFlow (硅基流动) — aggregator with many OSS models */
object SiliconFlowSupplier : Supplier {
    override val id = "siliconflow"
    override val displayName = "SiliconFlow (硅基流动)"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = "https://api.siliconflow.cn/v1/"
    override val suggestedModels = listOf(
        "deepseek-ai/DeepSeek-V3",
        "deepseek-ai/DeepSeek-R1",
        "Qwen/Qwen2.5-72B-Instruct",
        "Qwen/QwQ-32B-Preview",
        "meta-llama/Llama-3.3-70B-Instruct",
        "google/gemma-2-27b-it"
    )
    override val apiKeyRequired = true
    override val brandColor = 0xFFE0202A
    override val capabilities = setOf(Capability.STREAMING, Capability.MULTIMODAL, Capability.TOOL_CALLING, Capability.REASONING)
}

object CustomSupplier : Supplier {
    override val id = "custom"
    override val displayName = "自定义"
    override val category = SupplierCategory.OPENAI_COMPATIBLE
    override val defaultBaseUrl = ""
    override val suggestedModels = emptyList<String>()
    override val apiKeyRequired = true
    override val brandColor = 0xFF6B7280
    override val capabilities = setOf(Capability.STREAMING)
}

/**
 * Registry of all suppliers known to the app. Backed by Hilt so feature code
 * just injects [SupplierRegistry] and looks up by id.
 *
 * Order matters for the UI list — group by region/relevance:
 * 1. Default (own server)
 * 2. Chinese mainstream providers (智谱/通义/Kimi/豆包/千帆/星火/零一/混元/SiliconFlow)
 * 3. DeepSeek (separate because of popularity)
 * 4. International (OpenAI/Anthropic/Gemini)
 * 5. Local (Ollama)
 * 6. Custom (must be last for fallback)
 */
object BuiltinSuppliers {
    val all: List<Supplier> = listOf(
        DefaultServerSupplier,
        ZhipuGlmSupplier,
        QwenSupplier,
        KimiSupplier,
        DoubaoSupplier,
        DeepSeekSupplier,
        QianfanSupplier,
        SparkSupplier,
        LingyiSupplier,
        HunyuanSupplier,
        SiliconFlowSupplier,
        OpenAiSupplier,
        AnthropicSupplier,
        GeminiSupplier,
        OllamaSupplier,
        CustomSupplier
    )

    fun byId(id: String): Supplier? = all.firstOrNull { it.id == id }

    /** The supplier used when no other default exists. Always available, cannot be deleted. */
    val fallback: Supplier = DefaultServerSupplier
}
