package com.example.aichat.data.provider

import com.example.aichat.data.local.entity.ApiProfileEntity
import com.example.aichat.data.remote.AiApiService
import com.example.aichat.data.remote.AiStreamClient
import com.example.aichat.data.remote.Attachment as RemoteAttachment
import com.example.aichat.data.remote.ChatRemoteDataSource
import com.example.aichat.data.remote.dto.ChatRequestDto
import com.example.aichat.data.remote.dto.ContentPart
import com.example.aichat.data.remote.dto.ImageUrlData
import com.example.aichat.data.remote.dto.MessageDto
import com.example.aichat.domain.model.Message
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.io.IOException
import java.util.LinkedHashMap
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Creates (and caches) [LlmProvider] instances per [ApiProfileEntity].
 *
 * Why a factory instead of @Singleton Retrofit:
 *   The original code bound Retrofit as @Singleton with a single baseUrl read at
 *   startup. Changing the active API profile therefore required restarting the app.
 *   This factory builds a fresh OkHttp+Retrofit+AiApiService per profile and caches
 *   up to [MAX_CACHE_SIZE] of them, so switching profiles is instant.
 *
 * CONTRACT: [ApiProfileEntity.apiKeyEncrypted] is treated as PLAINTEXT by the
 * factory. Callers (ChatRemoteDataSource) MUST decrypt the stored ciphertext
 * via [com.example.aichat.data.security.ApiKeyEncryptor.decrypt] before passing
 * the entity in. The column name is preserved for migration continuity; the
 * plaintext never lands in the database.
 */
interface LlmProviderFactory {
    fun get(profile: ApiProfileEntity): LlmProvider
}

@Singleton
class LlmProviderFactoryImpl @Inject constructor(
    private val supplierRegistry: SupplierRegistry,
    private val streamClient: AiStreamClient
) : LlmProviderFactory {

    private val cache = object : LinkedHashMap<String, LlmProvider>(CACHE_INITIAL, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, LlmProvider>?): Boolean {
            return size > MAX_CACHE_SIZE
        }
    }

    @Synchronized
    override fun get(profile: ApiProfileEntity): LlmProvider {
        // Cache key MUST include updatedAt: OpenAiCompatibleProvider bakes the
        // profile's baseUrl/apiKey/model/fullUrlMode into its fields at
        // construction time, so a stale cache hit (same id, new contents)
        // would keep sending requests with the OLD config. Adding updatedAt
        // forces a fresh provider whenever the profile is upserted.
        val cacheKey = "${profile.id}@${profile.updatedAt}"
        return cache.getOrPut(cacheKey) {
            val supplier = supplierRegistry.byId(profile.supplierId) ?: CustomSupplier
            OpenAiCompatibleProvider(profile, supplier, streamClient)
        }
    }

    companion object {
        private const val MAX_CACHE_SIZE = 4
        private const val CACHE_INITIAL = 8
    }
}

/**
 * Handles every supplier whose protocol is OpenAI-compatible.
 *
 * Builds a dedicated OkHttp+Retrofit stack per profile so that baseUrl/apiKey
 * are baked in at creation time (no interceptor state, no race when switching
 * profiles mid-request).
 */
private class OpenAiCompatibleProvider(
    private val profile: ApiProfileEntity,
    private val supplier: Supplier,
    private val streamClient: AiStreamClient
) : LlmProvider {

    private val apiService: AiApiService = buildStack()

    override fun stream(
        messages: List<Message>,
        attachments: List<List<RemoteAttachment>>?,
        model: String?
    ): Flow<StreamEvent> = flow {
        val request = ChatRequestDto(
            model = model ?: profile.modelName,
            messages = if (attachments != null) {
                messages.mapIndexed { i, m -> m.toDto(attachments.getOrNull(i).orEmpty()) }
            } else {
                messages.map { it.toDto() }
            },
            stream = true
        )

        // v4.2.2: retry on transient IOException ("software caused connection
        // abort", "connection reset", network handoff, …). Crucially, retry
        // is gated on `!hasEmittedAnyDelta` — once the model has produced
        // content, restarting the request would re-generate from scratch
        // and produce duplicate text in the bubble. In that case we just
        // end the stream and let the UseCase finalize with what we have.
        //
        // Typical scenario this rescues: user switches apps / locks screen
        // during the model's "thinking" phase before the first token lands.
        // The :streaming process's foreground service keeps the process
        // alive but the socket still dies on aggressive OEM ROMs — retry
        // opens a fresh socket and usually succeeds.
        var hasEmittedAnyDelta = false
        var attempt = 0
        while (true) {
            val responseBody = try {
                apiService.streamChat(endpointPath, request)
            } catch (e: HttpException) {
                // Read the server's actual error body — Retrofit's HttpException.message()
                // is just "HTTP 400 Bad Request", which is useless for diagnosing
                // auth/quota/schema errors. The real reason ("messages field is
                // required", "Rate limit exceeded", "令牌已过期", ...) lives here.
                val raw = runCatching { e.response()?.errorBody()?.string() }.getOrNull()
                throw com.example.aichat.data.remote.ApiException(
                    "API error: HTTP ${e.code()} - ${raw ?: e.message()}", e
                )
            } catch (e: IOException) {
                // Pre-stream IOException (couldn't even establish connection):
                // retry up to MAX_STREAM_RETRY times with exponential backoff.
                attempt++
                if (attempt > MAX_STREAM_RETRY) {
                    throw com.example.aichat.data.remote.NetworkException(
                        "网络错误：${e.message}（已重试 $MAX_STREAM_RETRY 次）", e
                    )
                }
                delay(RETRY_BACKOFF_MS[attempt - 1])
                continue
            }

            try {
                streamClient.toEventFlow(responseBody).collect { event ->
                    emit(event)
                    if (event is StreamEvent.ContentDelta ||
                        event is StreamEvent.ReasoningDelta) {
                        hasEmittedAnyDelta = true
                    }
                }
                // Stream completed normally.
                return@flow
            } catch (e: kotlinx.coroutines.CancellationException) {
                // Cooperative cancellation — propagate, don't retry.
                throw e
            } catch (e: IOException) {
                // Mid-stream IOException. If we've already shown content, end
                // gracefully so the UseCase finalizes partial content as
                // INTERRUPTED rather than throwing + showing an error bubble.
                if (hasEmittedAnyDelta) {
                    return@flow
                }
                attempt++
                if (attempt > MAX_STREAM_RETRY) {
                    throw com.example.aichat.data.remote.NetworkException(
                        "网络错误：${e.message}（已重试 $MAX_STREAM_RETRY 次）", e
                    )
                }
                delay(RETRY_BACKOFF_MS[attempt - 1])
                // Loop back to re-issue the request.
            }
        }
    }.flowOn(Dispatchers.IO)

    /**
     * The `@Url` argument passed to [AiApiService.streamChat].
     *
     * - Legacy mode (fullUrlMode = false): relative path `chat/completions`
     *   resolved against the Retrofit baseUrl (which is the profile's
     *   versioned baseUrl with a trailing slash). Preserves the previous
     *   behaviour for every built-in supplier.
     * - Full-URL mode (fullUrlMode = true): the profile's baseUrl verbatim,
     *   treated as an absolute URL by Retrofit so the configured baseUrl is
     *   bypassed entirely.
     */
    private val endpointPath: String
        get() = if (profile.fullUrlMode) profile.baseUrl else "chat/completions"

    private fun buildStack(): AiApiService {
        // In fullUrlMode the absolute URL is supplied per-call via @Url, so
        // the Retrofit baseUrl only needs to be a syntactically valid placeholder
        // — derive it from the URL's origin to keep TLS/DNS warm for the right
        // host. In legacy mode the baseUrl IS the API root (versioned) and the
        // relative "chat/completions" path resolves against it.
        val baseUrl = if (profile.fullUrlMode) {
            deriveOriginWithTrailingSlash(profile.baseUrl)
        } else {
            profile.baseUrl.let { if (it.endsWith("/")) it else "$it/" }
        }

        // Logging: NONE in streaming path. Even BASIC level reads every response
        // chunk to print its metadata, which forces buffering and delays SSE
        // delivery to the parser → looks like the model is "slow" when it's
        // actually OkHttp holding back chunks for the logger.
        // Server error bodies are surfaced in-app via ApiException (see stream()).
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.NONE
        }
        val auth = Interceptor { chain ->
            val builder = chain.request().newBuilder()
                .addHeader("Authorization", "Bearer ${profile.apiKeyEncrypted}")
                .addHeader("Content-Type", "application/json")
                .addHeader("Accept", "text/event-stream")
                // Disable gzip on the SSE stream. If the server returns gzip'd
                // chunks OkHttp waits for a full compressed block before
                // decompressing — visible as "bursty" token delivery.
                .addHeader("Accept-Encoding", "identity")
                // Hint to intermediate proxies / CDN to disable buffering too.
                .addHeader("Cache-Control", "no-cache")
                .addHeader("Connection", "keep-alive")
            supplier.extraHeaders.forEach { (k, v) -> builder.addHeader(k, v) }
            chain.proceed(builder.build())
        }

        val client = OkHttpClient.Builder()
            .addInterceptor(auth)
            .addInterceptor(logging)
            .connectionPool(sharedConnectionPool)
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)   // no read timeout for streaming
            .writeTimeout(30, TimeUnit.SECONDS)
            .callTimeout(0, TimeUnit.SECONDS)   // no overall cap; stream stays open
            // v4.2.9: HTTP/2 ping frame every 30s. Keeps the SSE socket from
            // looking "idle" to NAT / OEM Doze network policy on Honor/EMUI,
            // which would otherwise RST the connection after ~60-120s of
            // silence between model reasoning and first content token.
            // For HTTP/1.1 endpoints this is a no-op (OkHttp just doesn't ping).
            .pingInterval(30, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()

        return retrofit.create(AiApiService::class.java)
    }

    companion object {
        // One shared pool across all profiles so DNS/TLS warmups persist when
        // the user switches profiles. Without this, switching profile = new
        // OkHttp = new pool = cold TLS handshake on the next request.
        private val sharedConnectionPool = okhttp3.ConnectionPool(
            maxIdleConnections = 5,
            keepAliveDuration = 5,
            TimeUnit.MINUTES
        )

        /**
         * SSE retry config — applied per-stream, only while no content has
         * been emitted yet (see stream() doc for rationale).
         */
        private const val MAX_STREAM_RETRY = 3
        private val RETRY_BACKOFF_MS = longArrayOf(1_000L, 2_000L, 4_000L)
    }

    private fun Message.toDto(): MessageDto = MessageDto(
        role = role.name.lowercase(),
        content = listOf(ContentPart.Text(text = content))
    )

    private fun Message.toDto(remoteAttachments: List<RemoteAttachment>): MessageDto {
        val parts = mutableListOf<ContentPart>()
        if (content.isNotBlank()) parts.add(ContentPart.Text(text = content))
        remoteAttachments.forEach {
            parts.add(ContentPart.ImageUrl(imageUrl = ImageUrlData(url = it.url)))
        }
        if (parts.isEmpty()) parts.add(ContentPart.Text(text = ""))
        return MessageDto(role = role.name.lowercase(), content = parts)
    }

    /**
     * Extracts `scheme://host[:port]/` from an absolute URL so Retrofit.Builder
     * has a valid baseUrl placeholder when [ApiProfileEntity.fullUrlMode] is on.
     * Falls back to the input (Retrofit will throw on build if invalid) when the
     * URL is malformed — better to fail loud than silently route to localhost.
     */
    private fun deriveOriginWithTrailingSlash(url: String): String {
        return runCatching {
            val u = java.net.URL(url)
            buildString {
                append(u.protocol).append("://").append(u.host)
                if (u.port != -1) append(':').append(u.port)
                append('/')
            }
        }.getOrDefault(url.let { if (it.endsWith("/")) it else "$it/" })
    }
}
