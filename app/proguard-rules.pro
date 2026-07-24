# ==========================================
# ProGuard / R8 keep rules
# ==========================================

# --- Retrofit ---
-keepattributes Signature, InnerClasses, EnclosingMethod
-keepattributes RuntimeVisibleAnnotations, RuntimeVisibleParameterAnnotations
-keepclassmembers,allowshrinking,allowobfuscation interface * {
    @retrofit2.http.* <methods>;
}
-dontwarn org.codehaus.mojo.animal_sniffer.ignoreannotations.IgnoreJRERequirement
-dontwarn javax.annotation.**
-keep,allowobfuscation,allowshrinking class retrofit2.Response
-keep,allowobfuscation,allowshrinking class kotlin.coroutines.Continuation

# --- OkHttp ---
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# --- Room ---
-keep class * extends androidx.room.RoomDatabase
-dontwarn androidx.room.paging.**

# --- Hilt ---
-keep class dagger.hilt.** { *; }
-keep class javax.inject.** { *; }
-dontwarn dagger.hilt.**

# --- Gson / JSON ---
-keepattributes Signature
-keep class com.google.gson.** { *; }
-keep class * implements com.google.gson.TypeSerializer
-keep class * implements com.google.gson.TypeDeserializer
-keep class com.example.aichat.data.remote.dto.** { *; }
-keep class com.example.aichat.domain.model.** { *; }

# --- Kotlin Coroutines ---
-dontwarn kotlinx.coroutines.**

# --- Compose ---
-dontwarn androidx.compose.**
