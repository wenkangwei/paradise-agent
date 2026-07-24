package com.example.aichat.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

data class ChatColorScheme(
    val userBubbleColor: Color,
    val onUserBubbleColor: Color,
    val aiBubbleColor: Color,
    val onAiBubbleColor: Color,
    val codeBackground: Color,
    val onCodeBackground: Color,
    val inlineCodeBackground: Color,
    val streamingCursor: Color,
)

private val LightChatColors = ChatColorScheme(
    userBubbleColor = Color(0xFF00696D),
    onUserBubbleColor = Color(0xFFFFFFFF),
    aiBubbleColor = Color(0xFFF0F4F3),
    onAiBubbleColor = Color(0xFF191C1C),
    codeBackground = Color(0xFF1E1E1E),
    onCodeBackground = Color(0xFFD4D4D4),
    inlineCodeBackground = Color(0xFFE2E8E7),
    streamingCursor = Color(0xFF00696D),
)

private val DarkChatColors = ChatColorScheme(
    userBubbleColor = Color(0xFF004F53),
    onUserBubbleColor = Color(0xFF6FF6FE),
    aiBubbleColor = Color(0xFF242929),
    onAiBubbleColor = Color(0xFFE0E3E2),
    codeBackground = Color(0xFF1A1A1A),
    onCodeBackground = Color(0xFFD4D4D4),
    inlineCodeBackground = Color(0xFF2D3333),
    streamingCursor = Color(0xFF4CD9E2),
)

val LocalChatColors = staticCompositionLocalOf { LightChatColors }

val MaterialTheme.chatColors: ChatColorScheme
    @Composable
    @ReadOnlyComposable
    get() = LocalChatColors.current

@Composable
fun provideChatColors(darkTheme: Boolean, content: @Composable () -> Unit) {
    val colors = if (darkTheme) DarkChatColors else LightChatColors
    androidx.compose.runtime.CompositionLocalProvider(LocalChatColors provides colors) {
        content()
    }
}
