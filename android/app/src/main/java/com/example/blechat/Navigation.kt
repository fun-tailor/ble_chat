package com.example.blechat

import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.ui.NavDisplay
import com.example.blechat.data.Repository
import com.example.blechat.ui.history.HistoryScreen
import com.example.blechat.ui.main.MainScreen
import com.example.blechat.ui.settings.SettingsScreen

@Composable
fun BleChatNavigation(
    initialSharedText: String? = null,
    onSharedTextConsumed: () -> Unit = {},
) {
    val backStack = rememberNavBackStack(Chat)
    val context = LocalContext.current
    val repo = remember { Repository.get(context) }

    NavDisplay(
        backStack = backStack,
        onBack = { backStack.removeLastOrNull() },
        entryProvider = entryProvider {
            entry<Chat> {
                MainScreen(
                    sharedText = initialSharedText,
                    onSharedTextConsumed = onSharedTextConsumed,
                    onOpenHistory = { backStack.add(History) },
                    onOpenSettings = { backStack.add(Settings) },
                    modifier = Modifier.safeDrawingPadding().padding(16.dp),
                )
            }
            entry<History> {
                HistoryScreen(repo = repo, onBack = { backStack.removeLastOrNull() })
            }
            entry<Settings> {
                SettingsScreen(repo = repo, onDone = { backStack.removeLastOrNull() })
            }
        },
    )
}
