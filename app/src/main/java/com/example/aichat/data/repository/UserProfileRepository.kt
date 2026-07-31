package com.example.aichat.data.repository

import android.content.Context
import android.content.SharedPreferences
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Simple user profile stored in SharedPreferences.
 * Sent to server via X-User-Profile header for prompt personalization.
 *
 * Max 200 chars total to keep prompt lean.
 */
data class UserProfile(
    val name: String = "",
    val description: String = "",
    val location: String = ""
) {
    /** Compact representation for HTTP header (max 200 chars). */
    fun toHeaderValue(): String = buildString {
        if (name.isNotBlank()) append("name=${name.take(20)}; ")
        if (description.isNotBlank()) append("desc=${description.take(80)}; ")
        if (location.isNotBlank()) append("loc=${location.take(30)}; ")
    }.trimEnd().take(200)

    companion object {
        val EMPTY = UserProfile()
    }
}

@Singleton
class UserProfileRepository @Inject constructor(
    @ApplicationContext private val context: Context
) {
    private val prefs: SharedPreferences
        get() = context.getSharedPreferences("user_profile", Context.MODE_PRIVATE)

    fun get(): UserProfile = UserProfile(
        name = prefs.getString("name", "") ?: "",
        description = prefs.getString("description", "") ?: "",
        location = prefs.getString("location", "") ?: ""
    )

    fun save(profile: UserProfile) {
        prefs.edit()
            .putString("name", profile.name)
            .putString("description", profile.description)
            .putString("location", profile.location)
            .apply()
    }
}
