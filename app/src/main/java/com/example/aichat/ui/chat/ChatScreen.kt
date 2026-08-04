package com.example.aichat.ui.chat

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.runtime.DisposableEffect
import androidx.compose.ui.draw.clip
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.aichat.data.repository.ApiProfile
import com.example.aichat.ui.chat.toolcard.FavoriteToolDialog
import com.example.aichat.ui.chat.toolcard.ToolCardFullScreen
import com.example.aichat.ui.chat.toolcard.ToolSharer
import com.example.aichat.ui.chat.toolcard.ToolType
import com.example.aichat.ui.chat.toolcard.deriveToolTitle
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    onNavigateToSettings: () -> Unit = {},
    viewModel: ChatViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    val speakingMessageId by viewModel.speakingMessageId.collectAsStateWithLifecycle()
    val listState = rememberLazyListState()
    val snackbarHostState = remember { SnackbarHostState() }
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val keyboard = LocalSoftwareKeyboardController.current
    val context = LocalContext.current

    // ---- Tool card sheet + favorite dialog state ----
    // Both are hoisted here (not in MessageBubble) so they survive bubble
    // recomposition and can route to ViewModel / FileProvider without
    // threading context through the message list.
    var fullScreenTool by remember {
        mutableStateOf<ToolCardTarget?>(null)
    }
    var favoriteDialog by remember {
        mutableStateOf<ToolCardTarget?>(null)
    }

    // v4.2.12 #5: multi-select share sheet. null = hidden; non-null = the
    // set of message ids to pre-check when the sheet opens. Tapping the
    // share icon on a bubble seeds this with that bubble's id alone; the
    // user can then check more bubbles inside the sheet before firing
    // ACTION_SEND. See [MultiShareSheet].
    var multiShareInitial by remember { mutableStateOf<Set<String>?>(null) }

    // Single share-action router. Wrapped in runCatching because share can
    // throw if no app handles the intent (rare on Android but possible on
    // stripped ROMs) — better to surface a snackbar than crash.
    val onShareTool: (String, ToolType) -> Unit = remember(context) {
        { content, type ->
            val title = deriveToolTitle(content, type)
            runCatching {
                ToolSharer.share(
                    context = context,
                    title = title,
                    content = content,
                    type = type
                )
            }.onFailure { e ->
                scope.launch {
                    snackbarHostState.showSnackbar(
                        message = "分享失败: ${e.message ?: "未知错误"}",
                        withDismissAction = true,
                        duration = androidx.compose.material3.SnackbarDuration.Short
                    )
                }
            }
        }
    }

    // Keep the screen on while a stream is in progress so the user can watch
    // the reply render without the device sleeping and tearing down the
    // connection. The ViewModel holds a PARTIAL_WAKE_LOCK for CPU; this
    // handles the display.
    DisposableEffect(uiState.isStreaming) {
        val window = (context as? android.app.Activity)?.window
        if (uiState.isStreaming) {
            window?.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
        onDispose {
            window?.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    // v4.2.4 smart auto-scroll: rewritten to fix the recurring "user
    // scrolled up but got yanked back during streaming" complaint.
    //
    // Root cause of the previous bug: `isStrictAtBottom` only checked that
    // the *index* of the last visible item was the last item. That's true
    // even when 99% of the last item is off-screen — so the moment a new
    // token grew the streaming bubble past the viewport, the user's "scroll
    // up by one screen" still reported `isStrictAtBottom = true` because
    // the very tail of the streaming bubble was peeking into view at the
    // bottom edge. The follow flag stayed armed → auto-scroll yanked back.
    //
    // New check: the LAST item's bottom edge must actually be within the
    // viewport (with a small tolerance for the keyboard inset / overscroll
    // glow). Only then is the user really "at the bottom".
    val isStrictAtBottom by remember {
        derivedStateOf {
            val info = listState.layoutInfo
            val total = uiState.messages.size
            if (total == 0) return@derivedStateOf true
            val last = info.visibleItemsInfo.lastOrNull() ?: return@derivedStateOf false
            last.index == total - 1 &&
                last.offset + last.size <= info.viewportEndOffset + BOTTOM_TOLERANCE_PX
        }
    }
    var userPinnedToBottom by rememberSaveable { mutableStateOf(true) }

    // Re-arm follow when the user reaches the bottom under their own steam.
    LaunchedEffect(isStrictAtBottom) {
        if (isStrictAtBottom) userPinnedToBottom = true
    }
    // Disable follow as soon as the user is NOT strictly at the bottom.
    // snapshotFlow + distinctUntilChanged avoids re-firing on every pixel
    // of intermediate scroll.
    LaunchedEffect(listState, uiState.messages.size) {
        snapshotFlow { isStrictAtBottom }
            .distinctUntilChanged()
            .collect { atBottom ->
                if (!atBottom) userPinnedToBottom = false
            }
    }

    // Drive auto-scroll from the flag instead of the live isAtBottom.
    LaunchedEffect(
        uiState.messages.lastOrNull()?.content,
        uiState.messages.size,
        userPinnedToBottom
    ) {
        if (uiState.messages.isNotEmpty() && userPinnedToBottom) {
            // animateScrollToItem with no offset scrolls the item to the
            // top of the viewport. For the last item, this naturally pins
            // it to the top — which is what we want when content is long
            // enough to push earlier messages off-screen. When the last
            // item is short, the LazyColumn shows it at the bottom of the
            // list area anyway (no scroll needed beyond the end).
            listState.animateScrollToItem(uiState.messages.lastIndex)
        }
    }

    // Show "scroll-to-bottom" button when the last visible item isn't the last message
    val showScrollDown by remember {
        derivedStateOf {
            val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1
            val total = uiState.messages.size
            total > 1 && lastVisible in 0 until (total - 1)
        }
    }
    val showScrollUp by remember {
        derivedStateOf { listState.firstVisibleItemIndex > 0 }
    }

    // If the user is about to wait for a streaming reply, remind them once
    // that keeping the app alive while the screen is off works best when the
    // app is excluded from battery optimisations.
    //
    // v4.2.3: gate with a session-scoped flag so the snackbar only fires on
    // the FIRST streaming reply of the user's session — previously it
    // nagged on every send. The flag is `rememberSaveable` so it survives
    // configuration changes; it does reset across process death, which is
    // the right behaviour (next launch = a new session = remind once more).
    var batteryPromptShownThisSession by rememberSaveable { mutableStateOf(false) }
    LaunchedEffect(uiState.isStreaming) {
        if (uiState.isStreaming && !batteryPromptShownThisSession) {
            batteryPromptShownThisSession = true
            val pm = context.getSystemService(android.content.Context.POWER_SERVICE) as android.os.PowerManager
            if (!pm.isIgnoringBatteryOptimizations(context.packageName)) {
                snackbarHostState.showSnackbar(
                    message = "建议关闭电池优化，息屏后台更稳定",
                    actionLabel = "去设置",
                    withDismissAction = true
                ).let { result ->
                    if (result == SnackbarResult.ActionPerformed) {
                        val intent = android.content.Intent(
                            android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS
                        )
                        runCatching { context.startActivity(intent) }
                    }
                }
            }
        }
    }

    // Handle one-time events
    LaunchedEffect(Unit) {
        viewModel.events.collect { event ->
            when (event) {
                is ChatEvent.ShowError -> {
                    val result = snackbarHostState.showSnackbar(
                        message = event.message,
                        actionLabel = event.retryAction?.let { "重试" } ?: "关闭",
                        withDismissAction = true,
                        duration = androidx.compose.material3.SnackbarDuration.Short
                    )
                    if (result == SnackbarResult.ActionPerformed) {
                        event.retryAction?.invoke()
                    }
                }
                is ChatEvent.MessageSent -> {
                    // No-op
                }
                is ChatEvent.NavigateToConversation -> {
                    // Future: navigate to conversation route
                }
            }
        }
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ChatListDrawer(
                conversations = uiState.conversations,
                currentConversationId = uiState.currentConversationId,
                favoriteTools = uiState.favoriteTools,
                onNewChat = {
                    viewModel.newChat()
                    scope.launch { drawerState.close() }
                },
                onSelectConversation = { id ->
                    viewModel.selectConversation(id)
                    scope.launch { drawerState.close() }
                },
                onDeleteConversation = { id ->
                    viewModel.deleteConversation(id)
                },
                onUseFavoriteTool = { tool ->
                    viewModel.useFavoriteTool(tool)
                    scope.launch { drawerState.close() }
                },
                onDeleteFavoriteTool = { id ->
                    viewModel.deleteFavoriteTool(id)
                },
                onOpenSettings = {
                    scope.launch { drawerState.close() }
                    onNavigateToSettings()
                },
                streamingConversationIds = uiState.streamingConversationIds,
                favoritesInitialExpanded = uiState.favoriteTools.isNotEmpty()
            )
        }
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = {
                        ProfileSelector(
                            profiles = uiState.profiles,
                            activeProfile = uiState.activeProfile,
                            conversationTitle = uiState.currentConversationTitle,
                            onSelect = { id -> viewModel.selectApiProfile(id) },
                            onManageProfiles = onNavigateToSettings
                        )
                    },
                    navigationIcon = {
                        IconButton(
                            onClick = { scope.launch { drawerState.open() } }
                        ) {
                            Icon(
                                imageVector = Icons.Filled.Menu,
                                contentDescription = "Conversation history"
                            )
                        }
                    },
                    actions = {
                        IconButton(onClick = { viewModel.newChat() }) {
                            Icon(
                                imageVector = Icons.Filled.Add,
                                contentDescription = "New conversation"
                            )
                        }
                        IconButton(onClick = onNavigateToSettings) {
                            Icon(
                                imageVector = Icons.Filled.Settings,
                                contentDescription = "Settings"
                            )
                        }
                    }
                )
            },
            // Intentionally NO bottomBar — input floats over chat with transparent
            // background, no divider line drawn by Scaffold.
            snackbarHost = {
                SnackbarHost(
                    hostState = snackbarHostState,
                    // Push above the floating input bar so the snackbar never hides it
                    modifier = Modifier.padding(bottom = 84.dp)
                )
            }
        ) { innerPadding ->
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
            ) {
                if (uiState.messages.isEmpty() && uiState.pendingAttachments.isEmpty()) {
                    EmptyState(
                        onSuggestionClick = { viewModel.sendMessage(it) },
                        modifier = Modifier.fillMaxSize()
                    )
                } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier
                            .fillMaxSize()
                            // Dismiss the IME on any vertical drag so the bottom of
                            // the list isn't hidden behind the keyboard-covered input pill.
                            .pointerInput(Unit) {
                                detectVerticalDragGestures { _, _ -> keyboard?.hide() }
                            },
                        // Reserve bottom space so messages aren't hidden behind the floating pill
                        contentPadding = PaddingValues(
                            start = 12.dp,
                            end = 12.dp,
                            top = 8.dp,
                            bottom = 96.dp
                        ),
                        verticalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        itemsIndexed(
                            items = uiState.messages,
                            key = { _, msg -> msg.id }
                        ) { index, message ->
                            // Time divider — WeChat-style. Show above the
                            // message when:
                            //   - it's the first message, OR
                            //   - the previous message was > 5 minutes ago, OR
                            //   - the day changed since the previous message
                            val prev = uiState.messages.getOrNull(index - 1)
                            if (prev == null ||
                                message.timestamp - prev.timestamp >= FIVE_MINUTES_MS ||
                                !isSameDay(message.timestamp, prev.timestamp)) {
                                TimeDivider(timestamp = message.timestamp)
                            }
                            MessageBubble(
                                message = message,
                                onReact = { r -> viewModel.setMessageReaction(message.id, r) },
                                onRetry = { viewModel.retryMessage(message.id) },
                                onOpenTool = { content, type ->
                                    fullScreenTool = ToolCardTarget(
                                        title = deriveToolTitle(content, type),
                                        content = content,
                                        type = type
                                    )
                                },
                                onCollectTool = { content, type ->
                                    favoriteDialog = ToolCardTarget(
                                        title = deriveToolTitle(content, type),
                                        content = content,
                                        type = type
                                    )
                                },
                                onShareTool = onShareTool,
                                onShareMessage = {
                                    multiShareInitial = setOf(message.id)
                                    viewModel.onMessageShared(message.id)
                                },
                                onSpeak = { viewModel.speakMessage(message.id, message.content) },
                                isSpeaking = speakingMessageId == message.id
                            )
                        }
                    }
                }

                // Floating scroll-to-top button (top-right of the list area)
                AnimatedVisibility(
                    visible = showScrollUp,
                    enter = fadeIn() + scaleIn(initialScale = 0.7f),
                    exit = fadeOut() + scaleOut(targetScale = 0.7f),
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(top = 8.dp, end = 16.dp)
                ) {
                    ScrollFab(
                        icon = Icons.Filled.ArrowUpward,
                        contentDescription = "回到顶部",
                        onClick = {
                            scope.launch { listState.animateScrollToItem(0) }
                        }
                    )
                }

                // Floating scroll-to-bottom button (above the input pill)
                AnimatedVisibility(
                    visible = showScrollDown,
                    enter = fadeIn() + scaleIn(initialScale = 0.7f),
                    exit = fadeOut() + scaleOut(targetScale = 0.7f),
                    modifier = Modifier
                        .align(Alignment.BottomEnd)
                        .padding(bottom = 84.dp, end = 16.dp)
                ) {
                    ScrollFab(
                        icon = Icons.Filled.ArrowDownward,
                        contentDescription = "回到底部",
                        onClick = {
                            if (uiState.messages.isNotEmpty()) {
                                scope.launch {
                                    listState.animateScrollToItem(uiState.messages.lastIndex)
                                }
                            }
                        }
                    )
                }

                // Floating input pill — overlaid at the bottom of the chat
                ChatInputBar(
                    isLoading = uiState.isLoading,
                    onSend = { text, attachments ->
                        viewModel.sendMessage(text, attachments)
                    },
                    onStop = { viewModel.stopGenerating() },
                    pendingAttachments = uiState.pendingAttachments,
                    onAddAttachment = { uri, mimeType ->
                        viewModel.addPendingAttachment(uri, mimeType)
                    },
                    onRemoveAttachment = { id ->
                        viewModel.removePendingAttachment(id)
                    },
                    serverBaseUrl = uiState.activeProfile?.baseUrl.orEmpty()
                        .let { runCatching {
                            val u = java.net.URL(it)
                            "${u.protocol}://${u.host}${if (u.port > 0) ":${u.port}" else ""}"
                        }.getOrDefault("") },
                    pendingInput = uiState.pendingInput,
                    onPendingInputConsumed = { viewModel.consumePendingInput() },
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(horizontal = 12.dp, vertical = 10.dp)
                )

                // ---- Tool-card overlays (hoisted at screen scope) ----
                // Rendered inside the content Box so they layer above the
                // chat list but below the drawer.
                fullScreenTool?.let { target ->
                    ToolCardFullScreen(
                        title = target.title,
                        content = target.content,
                        type = target.type,
                        onDismiss = { fullScreenTool = null },
                        onCollect = {
                            // Hand off to the favorite dialog with the same payload.
                            favoriteDialog = target
                            fullScreenTool = null
                        },
                        onShare = {
                            onShareTool(target.content, target.type)
                        }
                    )
                }
                favoriteDialog?.let { target ->
                    FavoriteToolDialog(
                        initialTitle = target.title,
                        type = target.type,
                        preview = target.content,
                        onConfirm = { finalTitle ->
                            viewModel.saveFavoriteTool(
                                content = target.content,
                                type = target.type,
                                title = finalTitle
                            )
                            favoriteDialog = null
                            scope.launch {
                                snackbarHostState.showSnackbar(
                                    message = "已收藏：$finalTitle",
                                    duration = androidx.compose.material3.SnackbarDuration.Short
                                )
                            }
                        },
                        onDismiss = { favoriteDialog = null }
                    )
                }

                // v4.2.12 #5: multi-select share sheet. Pre-seeds with
                // the message the user tapped share on; user can add more
                // before firing ACTION_SEND.
                multiShareInitial?.let { initialIds ->
                    MultiShareSheet(
                        messages = uiState.messages,
                        initialSelectedIds = initialIds,
                        onDismiss = { multiShareInitial = null }
                    )
                }
            }
        }
    }
}

/**
 * Hoisted payload used to drive both the fullscreen tool sheet and the
 * favorite-tool naming dialog. Same shape, different lifecycles.
 */
private data class ToolCardTarget(
    val title: String,
    val content: String,
    val type: ToolType
)

@Composable
private fun ScrollFab(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    contentDescription: String,
    onClick: () -> Unit
) {
    Surface(
        shape = CircleShape,
        color = MaterialTheme.colorScheme.primaryContainer,
        tonalElevation = 3.dp,
        shadowElevation = 4.dp,
        modifier = Modifier.size(40.dp)
    ) {
        IconButton(onClick = onClick) {
            Icon(
                imageVector = icon,
                contentDescription = contentDescription,
                tint = MaterialTheme.colorScheme.onPrimaryContainer
            )
        }
    }
}

/**
 * TopAppBar title — two-line Kimi-style layout:
 *   - Primary: conversation topic (or profile title if no conversation yet)
 *   - Subtitle: profile title · model name
 * Tapping the title opens the API profile switcher dropdown.
 */
@Composable
private fun ProfileSelector(
    profiles: List<ApiProfile>,
    activeProfile: ApiProfile?,
    conversationTitle: String?,
    onSelect: (String) -> Unit,
    onManageProfiles: () -> Unit
) {
    var expanded by remember { mutableStateOf(false) }
    val profileTitle = activeProfile?.title ?: "未配置 API"
    val modelName = activeProfile?.modelName
    val primary = conversationTitle?.takeIf { it.isNotBlank() } ?: profileTitle
    // When a conversation title is shown, subtitle lists the profile too; otherwise
    // subtitle is just the model name (so primary and subtitle don't duplicate).
    val secondary = if (conversationTitle.isNullOrBlank()) {
        modelName
    } else {
        listOfNotNull(profileTitle, modelName)
            .takeIf { it.isNotEmpty() }
            ?.joinToString(" · ")
    }

    Box {
        Column(
            modifier = Modifier
                .clip(RoundedCornerShape(8.dp))
                .clickable { expanded = true }
                .padding(end = 4.dp)
        ) {
            Text(
                text = primary,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            if (!secondary.isNullOrBlank()) {
                Spacer(Modifier.size(2.dp))
                Text(
                    text = secondary,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false }
        ) {
            if (profiles.isEmpty()) {
                DropdownMenuItem(
                    text = { Text("尚无配置，点此新建") },
                    onClick = { expanded = false; onManageProfiles() }
                )
            } else {
                profiles.forEach { p ->
                    DropdownMenuItem(
                        text = {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    text = p.title,
                                    style = MaterialTheme.typography.bodyMedium,
                                    fontWeight = if (p.id == activeProfile?.id) FontWeight.SemiBold
                                                 else FontWeight.Normal
                                )
                                Spacer(Modifier.size(8.dp))
                                Text(
                                    text = "· ${p.modelName}",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        },
                        onClick = {
                            onSelect(p.id)
                            expanded = false
                        }
                    )
                }
                DropdownMenuItem(
                    text = { Text("管理 API 配置 →") },
                    onClick = {
                        expanded = false
                        onManageProfiles()
                    }
                )
            }
        }
    }
}

/**
 * Time divider shown between chat messages — WeChat-style centered pill
 * with a relative timestamp. Inserted by the LazyColumn when the gap
 * since the previous message exceeds [FIVE_MINUTES_MS] or the day changes.
 */
@Composable
private fun TimeDivider(timestamp: Long, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
        contentAlignment = Alignment.Center
    ) {
        Surface(
            color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.8f),
            shape = RoundedCornerShape(8.dp)
        ) {
            Text(
                text = remember(timestamp) { formatChatDividerTime(timestamp) },
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 10.dp, vertical = 3.dp)
            )
        }
    }
}

/**
 * Same day (calendar time, ignoring hours/minutes/seconds).
 */
private fun isSameDay(a: Long, b: Long): Boolean {
    val ca = Calendar.getInstance().apply { timeInMillis = a }
    val cb = Calendar.getInstance().apply { timeInMillis = b }
    return ca.get(Calendar.YEAR) == cb.get(Calendar.YEAR) &&
        ca.get(Calendar.DAY_OF_YEAR) == cb.get(Calendar.DAY_OF_YEAR)
}

/**
 * Format a timestamp for the chat divider — always shows clock + granularity
 * grows for older messages:
 *   - Today:           "HH:mm"
 *   - Yesterday:       "昨天 HH:mm"
 *   - Day before y.:   "前天 HH:mm"
 *   - This year:       "MM-dd HH:mm"
 *   - Older:           "yyyy-MM-dd HH:mm"
 */
private fun formatChatDividerTime(timestamp: Long): String {
    val now = Calendar.getInstance()
    val msg = Calendar.getInstance().apply { timeInMillis = timestamp }
    val sameYear = now.get(Calendar.YEAR) == msg.get(Calendar.YEAR)
    val dayDiff = now.get(Calendar.DAY_OF_YEAR) - msg.get(Calendar.DAY_OF_YEAR)
    val isSameDay = sameYear && dayDiff == 0
    val isYesterday = sameYear && dayDiff == 1
    val isDayBeforeYesterday = sameYear && dayDiff == 2
    val time = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(timestamp))
    return when {
        isSameDay -> time
        isYesterday -> "昨天 $time"
        isDayBeforeYesterday -> "前天 $time"
        sameYear -> SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(timestamp))
        else -> SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date(timestamp))
    }
}

/** Minimum gap between adjacent messages to merit a time divider. */
private const val FIVE_MINUTES_MS = 5L * 60 * 1000

/**
 * Pixel tolerance for the "user is at the bottom" check in [ChatScreen].
 * Anything within this distance from the viewport's bottom edge counts
 * as "at bottom" — absorbs the LazyColumn's overscroll glow, navigation
 * bar inset, and small floating-button overlap without classifying the
 * user as "scrolled away".
 */
private const val BOTTOM_TOLERANCE_PX = 96
