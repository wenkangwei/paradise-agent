package com.example.aichat.ui.chat

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
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

    // Auto-scroll to bottom when new messages arrive or streaming content updates
    LaunchedEffect(uiState.messages.lastOrNull()?.content, uiState.messages.size) {
        if (uiState.messages.isNotEmpty()) {
            listState.animateScrollToItem(uiState.messages.lastIndex)
        }
    }

    // Handle one-time events
    LaunchedEffect(Unit) {
        viewModel.events.collect { event ->
            when (event) {
                is ChatEvent.ShowError -> {
                    val result = snackbarHostState.showSnackbar(
                        message = event.message,
                        actionLabel = event.retryAction?.let { "Retry" },
                        withDismissAction = event.retryAction == null
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
            snackbarHost = { SnackbarHost(snackbarHostState) }
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
                        modifier = Modifier.fillMaxSize(),
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
                            MessageBubble(message = message)
                        }
                    }
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

/**
 * Replaces the static "AI Chat" title with a dropdown that:
 *   - shows the active profile title + model
 *   - on tap, lists all configured profiles (live-updates as user adds/edits)
 *   - clicking a profile switches the active one instantly (no restart —
 *     LlmProviderFactory cache invalidates by profile id)
 *   - "管理 API 配置" item navigates to the settings page
 */
@Composable
private fun ProfileSelector(
    profiles: List<ApiProfile>,
    activeProfile: ApiProfile?,
    onSelect: (String) -> Unit,
    onManageProfiles: () -> Unit
) {
    var expanded by remember { mutableStateOf(false) }
    val title = activeProfile?.title ?: "未配置 API"
    val subtitle = activeProfile?.modelName

    Box {
        Row(
            modifier = Modifier
                .clip(RoundedCornerShape(8.dp))
                .clickable { expanded = true }
                .padding(end = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold
            )
            if (subtitle.isNullOrBlank().not()) {
                Spacer(Modifier.size(2.dp))
                Text(
                    text = "· $subtitle",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Icon(
                imageVector = Icons.Filled.ArrowDropDown,
                contentDescription = "切换 API 配置",
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
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
