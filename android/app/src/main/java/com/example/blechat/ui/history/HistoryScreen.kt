package com.example.blechat.ui.history

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.example.blechat.data.Repository
import com.example.blechat.data.db.MessageEntity
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 历史对话。对应 PC 端 SPEC §7「历史对话框」的 v1 子集：
 * 按天分组 + 文本搜索 + 删除。导出 JSON 放到后续版本。
 */
@Composable
fun HistoryScreen(
    repo: Repository,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var query by remember { mutableStateOf("") }
    var rows by remember { mutableStateOf<List<MessageEntity>>(emptyList()) }
    val scope = rememberCoroutineScope()

    suspend fun reload() {
        rows = if (query.isBlank()) repo.recent(null) else repo.search(null, query)
    }

    LaunchedEffect(query) { reload() }

    Column(modifier = modifier.fillMaxSize().padding(16.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text("历史消息", style = MaterialTheme.typography.headlineSmall)
            TextButton(onClick = onBack) { Text("返回") }
        }
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            singleLine = true,
            placeholder = { Text("搜索文本 / 文件名") },
            modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
        )
        if (rows.isEmpty()) {
            Text(
                "没有历史记录",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 24.dp),
            )
        }
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            var lastDay = ""
            items(rows, key = { it.id }) { row ->
                val day = dayOf(row.createdAt)
                if (day != lastDay) {
                    lastDay = day
                    Text(
                        day,
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                    HorizontalDivider()
                }
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        text = "[${if (row.direction == "out") "我" else row.peerName ?: "Host"}] " +
                            row.textOrPlaceholder(),
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.weight(1f, fill = true),
                    )
                    Text(
                        timeOf(row.createdAt),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    TextButton(onClick = {
                        scope.launch {
                            repo.deleteMessages(listOf(row.id))
                            reload()
                        }
                    }) { Text("删") }
                }
            }
        }
    }
}

private val dayFormat = SimpleDateFormat("yyyy-MM-dd", Locale.getDefault())
private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

private fun dayOf(epochMs: Long): String = dayFormat.format(Date(epochMs))
private fun timeOf(epochMs: Long): String = timeFormat.format(Date(epochMs))
