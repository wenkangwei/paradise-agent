package com.example.aichat.feature.profile

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.example.aichat.data.repository.UserProfile
import com.example.aichat.data.repository.UserProfileRepository

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun UserProfilePage(onNavigateBack: () -> Unit) {
    val context = LocalContext.current
    val repository = remember { UserProfileRepository(context) }
    val profile = remember { repository.get() }
    var name by remember { mutableStateOf(profile.name) }
    var description by remember { mutableStateOf(profile.description) }
    var location by remember { mutableStateOf(profile.location) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("用户信息") },
                navigationIcon = {
                    IconButton(onClick = onNavigateBack) {
                        Icon(Icons.Filled.ArrowBack, "返回")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Text("设置你的个人信息，AI 会据此提供更个性化的回复",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)

            OutlinedTextField(
                value = name,
                onValueChange = { if (it.length <= 20) name = it },
                label = { Text("名字/昵称（最多20字）") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                supportingText = { Text("${name.length}/20") }
            )

            OutlinedTextField(
                value = description,
                onValueChange = { if (it.length <= 80) description = it },
                label = { Text("个人描述（最多80字）") },
                minLines = 2,
                maxLines = 3,
                modifier = Modifier.fillMaxWidth(),
                supportingText = { Text("${description.length}/80") }
            )

            OutlinedTextField(
                value = location,
                onValueChange = { if (it.length <= 30) location = it },
                label = { Text("所在地（最多30字）") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                supportingText = { Text("${location.length}/30") }
            )

            Button(
                onClick = {
                    repository.save(UserProfile(name, description, location))
                    onNavigateBack()
                },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("保存")
            }
        }
    }
}
