package com.example.aichat.data.local

import androidx.room.TypeConverter
import com.example.aichat.domain.model.Attachment
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken

class AttachmentConverter {

    private val gson = Gson()

    @TypeConverter
    fun fromAttachmentList(list: List<Attachment>): String = gson.toJson(list)

    @TypeConverter
    fun toAttachmentList(value: String): List<Attachment> {
        if (value.isBlank()) return emptyList()
        val type = object : TypeToken<List<Attachment>>() {}.type
        return runCatching { gson.fromJson(value, type) }.getOrDefault(emptyList())
    }
}
