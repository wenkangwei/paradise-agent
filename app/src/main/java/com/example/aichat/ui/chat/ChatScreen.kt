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
import androidx.compose.foundation.lazy.items
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
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    onNavigateToSettings: () -> Unit = {},
    viewModel: ChatViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    val listState = rememberLazyListState()
    val snackbarHostState = remember { SnackbarHostState() }
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val keyboard = LocalSoftwareKeyboardController.current
    val context = LocalContext.current

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

    // Auto-scroll to bottom when new messages arrive or streaming content updates
    LaunchedEffect(uiState.messages.lastOrNull()?.content, uiState.messages.size) {
        if (uiState.messages.isNotEmpty()) {
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
    LaunchedEffect(uiState.isStreaming) {
        if (uiState.isStreaming) {
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
                onOpenSettings = {
                    scope.launch { drawerState.close() }
                    onNavigateToSettings()
                }
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
                        items(
                            items = uiState.messages,
                            key = { it.id }
                        ) { message ->
                            MessageBubble(
                                message = message,
                                onReact = { r -> viewModel.setMessageReaction(message.id, r) }
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
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .padding(horizontal = 12.dp, vertical = 10.dp)
                )
            }
        }
    }
}

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
