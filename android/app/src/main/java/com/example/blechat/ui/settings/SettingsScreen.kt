package com.example.blechat.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.example.blechat.data.Repository

/** 昵称设置。对应 PC 端 `networks.json` 里的 `identity.name`。 */
@Composable
fun SettingsScreen(
    repo: Repository,
    onDone: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var name by remember { mutableStateOf(repo.prefs.displayName) }

    Column(
        modifier = modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("设置", style = MaterialTheme.typography.headlineSmall)
        OutlinedTextField(
            value = name,
            onValueChange = { name = it.take(32) },
            label = { Text("昵称") },
            singleLine = true,
            supportingText = { Text("显示在对方的消息列表里；最多 32 字符") },
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "device_id：${repo.prefs.deviceId}\n（只读，用于识别本机）",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Button(
            onClick = {
                repo.prefs.displayName = name
                onDone()
            },
        ) {
            Text("保存")
        }
    }
}
