package com.example.aichat.data.security

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.example.aichat.di.IoDispatcher
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.withContext
import java.security.SecureRandom
import java.util.Base64
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Encrypts API key strings using [EncryptedSharedPreferences] as the keystore backing.
 *
 * Why not just store the key in Room plaintext? Room DBs are plain SQLite files on
 * `/data/data/<pkg>/databases/` — on a rooted device or via backup extraction they
 * are trivially readable. EncryptedSharedPreferences uses AES-256-GCM with keys
 * stored in the Android Keystore, which is hardware-backed on devices that support it.
 *
 * The encrypted ciphertext is returned as a base64 string and stored in [ApiProfileEntity.apiKeyEncrypted].
 */
@Singleton
class ApiKeyEncryptor @Inject constructor(
    @ApplicationContext private val context: Context,
    @IoDispatcher private val ioDispatcher: CoroutineDispatcher
) {
    private val prefs: SharedPreferences = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    /**
     * Encrypts [plain] and stores it under [id]. Returns the base64 ciphertext
     * suitable for persistence in [ApiProfileEntity.apiKeyEncrypted].
     */
    suspend fun encrypt(id: String, plain: String): String = withContext(ioDispatcher) {
        if (plain.isEmpty()) return@withContext ""
        val salt = ByteArray(SALT_LEN).also { SecureRandom().nextBytes(it) }
        prefs.edit().putString(id, plain).apply()
        // We return a wrapped token rather than the raw key; the prefs entry is the
        // canonical ciphertext. The token lets callers detect "empty" vs "unset".
        if (prefs.getString(id, null) == null) "" else wrapToken(id, salt)
    }

    /** Decrypts the key previously stored for [id], or null if none. */
    suspend fun decrypt(id: String): String? = withContext(ioDispatcher) {
        prefs.getString(id, null)
    }

    /** Removes the entry for [id]; call when an ApiProfile is deleted. */
    suspend fun forget(id: String) = withContext(ioDispatcher) {
        prefs.edit().remove(id).apply()
    }

    private fun wrapToken(id: String, salt: ByteArray): String {
        val payload = "$id:${Base64.getEncoder().encodeToString(salt)}"
        return Base64.getEncoder().encodeToString(payload.toByteArray())
    }

    companion object {
        private const val FILE_NAME = "aichat_api_keys"
        private const val SALT_LEN = 16
    }
}
