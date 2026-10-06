package com.example.blechat.ui.main

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.Manifest
import android.content.pm.PackageManager
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.blechat.ble.ScannedDevice
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(
    sharedText: String?,
    modifier: Modifier = Modifier,
    onSharedTextConsumed: () -> Unit = {},
    onOpenHistory: () -> Unit = {},
    onOpenSettings: () -> Unit = {},
    vm: MainViewModel = viewModel(),
) {
    val phase by vm.phase.collectAsStateWithLifecycle()
    val status by vm.status.collectAsStateWithLifecycle()
    val messages by vm.messages.collectAsStateWithLifecycle()
    val results by vm.results.collectAsStateWithLifecycle()
    val composer by vm.composer.collectAsStateWithLifecycle()
    val peerName by vm.peerName.collectAsStateWithLifecycle()
    val passwordRequest by vm.passwordRequest.collectAsStateWithLifecycle()
    val diagnostics by vm.diagnostics.collectAsStateWithLifecycle()
    val failureHint by vm.hint.collectAsStateWithLifecycle()
    val bondFirst by vm.bondFirst.collectAsStateWithLifecycle()

    val snackbarHostState = remember { SnackbarHostState() }
    val listState = rememberLazyListState()
    val context = LocalContext.current
    var macDialog by remember { mutableStateOf(false) }
    var macInput by remember { mutableStateOf("") }
    var diagDialog by remember { mutableStateOf(false) }

    // ---- ACTION_SEND 进来的文本：预填输入框，消费掉就清空（避免二次进屏重复追加）----
    LaunchedEffect(sharedText) {
        if (!sharedText.isNullOrBlank()) {
            vm.onSharedText(sharedText)
            onSharedTextConsumed()
        }
    }

    // ---- 提示条 ----
    LaunchedEffect(Unit) {
        vm.snackbar.collect { snackbarHostState.showSnackbar(it) }
    }

    // ---- 消息列表自动滚到底 ----
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) listState.animateScrollToItem(messages.size - 1)
    }

    // ---- 权限：扫描要 BLUETOOTH_SCAN，连接要 BLUETOOTH_CONNECT ----
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { grants ->
        val scan = grants[Manifest.permission.BLUETOOTH_SCAN] == true
        val conn = grants[Manifest.permission.BLUETOOTH_CONNECT] == true
        if (scan && conn) vm.startScan() else vm.onPermissionDenied()
    }

    fun requirePermissionsThenScan() {
        val need = arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
        ).filter {
            ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
        }
        if (need.isEmpty()) vm.startScan() else permissionLauncher.launch(need.toTypedArray())
    }

    Scaffold(
        modifier = modifier.fillMaxSize().imePadding(),
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("BLE Chat", style = MaterialTheme.typography.titleMedium)
                        Text(
                            text = status,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                actions = {
                    StatusDot(phase)
                    Spacer(Modifier.width(8.dp))
                    if (diagnostics != null) {
                        // 连接失败时把「到底卡在哪一步」摆到明面上：点开就是一整份病历，
                        // 可一键复制发出来（不用再让用户去抓 logcat）。
                        TextButton(onClick = { diagDialog = true }) { Text("诊断") }
                    }
                    TextButton(onClick = onOpenHistory) { Text("历史") }
                    TextButton(onClick = onOpenSettings) { Text("设置") }
                    if (phase == Phase.READY) {
                        TextButton(onClick = { vm.disconnect("手动断开") }) { Text("断开") }
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier.padding(padding).fillMaxSize(),
        ) {
            // ---------------- 消息区 ----------------
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 8.dp),
            ) {
                items(messages, key = { it.key }) { Bubble(it) }
                if (messages.isEmpty()) {
                    item {
                        Box(
                            modifier = Modifier.fillMaxWidth().padding(top = 48.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(
                                text = if (phase == Phase.READY) "还没有消息，打个招呼吧" else "点「扫描」找 Host，或按 MAC 直连",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }

            // ---------------- 连接区（非 READY 时才显示） ----------------
            if (phase != Phase.READY) {
                ConnectionPanel(
                    phase = phase,
                    results = results,
                    onScan = ::requirePermissionsThenScan,
                    onStop = vm::stopScan,
                    onPick = { vm.connect(it) },
                    onManual = { macDialog = true },
                    reconnectHint = vm.localHostNameHint() ?: vm.localAddressHint(),
                    onReconnect = { vm.reconnectLast() },
                    failureHint = failureHint,
                    onShowDiagnostics = { diagDialog = true },
                    hasDiagnostics = diagnostics != null,
                    bondFirst = bondFirst,
                    onBondFirstChange = vm::setBondFirst,
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp),
                )
            }

            // ---------------- 输入区 ----------------
            Row(
                modifier = Modifier.fillMaxWidth().padding(12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = composer,
                    onValueChange = vm::onComposeChanged,
                    modifier = Modifier.weight(1f),
                    placeholder = { Text(if (phase == Phase.READY) "输入消息…" else "先连上才能发（可先写好）") },
                    maxLines = 4,
                )
                Spacer(Modifier.width(8.dp))
                Button(onClick = vm::send, enabled = composer.isNotBlank()) {
                    Text("发送")
                }
            }
        }
    }

    // ---------------- 按 MAC 直连 ----------------
    if (macDialog) {
        AlertDialog(
            onDismissRequest = { macDialog = false },
            title = { Text("按 MAC 地址连接") },
            text = {
                OutlinedTextField(
                    value = macInput,
                    onValueChange = { macInput = it },
                    singleLine = true,
                    placeholder = { Text("AA:BB:CC:DD:EE:FF") },
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    macDialog = false
                    vm.connectByMac(macInput)
                }) { Text("连接") }
            },
            dismissButton = {
                TextButton(onClick = { macDialog = false }) { Text("取消") }
            },
        )
    }

    // ---------------- 连接诊断 ----------------
    if (diagDialog && diagnostics != null) {
        AlertDialog(
            onDismissRequest = { diagDialog = false },
            title = { Text("连接诊断") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        "下面这一次连接尝试的完整过程。发给开发者即可定位问题。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    SelectionContainer {
                        Text(
                            text = diagnostics ?: "",
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                            modifier = Modifier
                                .fillMaxWidth()
                                .heightIn(max = 360.dp)
                                .verticalScroll(rememberScrollState()),
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val cm = context.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                        as android.content.ClipboardManager
                    cm.setPrimaryClip(
                        android.content.ClipData.newPlainText("BLE Chat 诊断", diagnostics ?: ""),
                    )
                    diagDialog = false
                }) { Text("复制") }
            },
            dismissButton = {
                Row {
                    // 配对是双向记录：PC 侧忘了手机、手机上还留着，就会一直尝试加密然后失败。
                    // 这种"半配对"只能清掉重来，所以把入口放在诊断旁边。
                    TextButton(onClick = vm::forgetBond) { Text("取消配对") }
                    TextButton(onClick = { diagDialog = false }) { Text("关闭") }
                }
            },
        )
    }

    // ---------------- 口令框 ----------------
    passwordRequest?.let { req ->
        var value by remember(req) { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = vm::cancelPassword,
            title = { Text("输入共享密码") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        "正在加入「${req.hostName}」。密码由 Host 创建网络时设置。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    OutlinedTextField(
                        value = value,
                        onValueChange = { value = it },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        placeholder = { Text("至少 4 位") },
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = { vm.submitPassword(value) }) { Text("确定") }
            },
            dismissButton = {
                TextButton(onClick = vm::cancelPassword) { Text("取消") }
            },
        )
    }
}

// ------------------------------------------------------------------ 局部组件

@Composable
private fun StatusDot(phase: Phase) {
    val color = when (phase) {
        Phase.READY -> androidx.compose.material3.MaterialTheme.colorScheme.primary
        Phase.CONNECTING, Phase.HANDSHAKE, Phase.SCANNING ->
            androidx.compose.material3.MaterialTheme.colorScheme.tertiary
        Phase.CLOSED -> androidx.compose.material3.MaterialTheme.colorScheme.error
        Phase.IDLE -> androidx.compose.material3.MaterialTheme.colorScheme.outline
    }
    Box(
        modifier = Modifier
            .width(10.dp)
            .height(10.dp)
            .background(color, RoundedCornerShape(50)),
    )
}

@Composable
private fun ConnectionPanel(
    phase: Phase,
    results: List<ScannedDevice>,
    onScan: () -> Unit,
    onStop: () -> Unit,
    onPick: (ScannedDevice) -> Unit,
    onManual: () -> Unit,
    reconnectHint: String?,
    onReconnect: () -> Unit,
    failureHint: String?,
    onShowDiagnostics: () -> Unit,
    hasDiagnostics: Boolean,
    bondFirst: Boolean,
    onBondFirstChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(
        modifier = modifier,
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilledTonalButton(
                    onClick = if (phase == Phase.SCANNING) onStop else onScan,
                    modifier = Modifier.weight(1f),
                ) {
                    Text(if (phase == Phase.SCANNING) "停止扫描" else "扫描 Host")
                }
                OutlinedButton(onClick = onManual, modifier = Modifier.weight(1f)) {
                    Text("按 MAC 连接")
                }
            }
            if (reconnectHint != null) {
                OutlinedButton(onClick = onReconnect, modifier = Modifier.fillMaxWidth()) {
                    Text("重连上次 $reconnectHint")
                }
            }
            when (phase) {
                Phase.SCANNING -> Text("扫描中…", style = MaterialTheme.typography.bodySmall)
                Phase.CONNECTING, Phase.HANDSHAKE -> Text("连接中…", style = MaterialTheme.typography.bodySmall)
                else -> Unit
            }
            // 失败之后不只是"失败"两个字：把**该动哪一端**直接摆出来。
            // snackbar 会消失，这一块会一直留着直到下一次连接。
            if (failureHint != null && phase != Phase.READY) {
                Text(
                    text = failureHint,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            // BLE 配对开关。默认开：手机和 PC 的 LE 地址都是会轮换的随机地址，
            // 只有配对过（交换 IRK）双方才能认出彼此 —— 而"认不出"的表现正好是
            // 「LL 连得上、ATT 一个字节都不回」。
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("连接前先配对", style = MaterialTheme.typography.bodyMedium)
                    Text(
                        "配对后双方才认得出彼此轮换的蓝牙地址。" +
                            "App 的共享密码是另一回事，它在链路就绪之后才问。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(checked = bondFirst, onCheckedChange = onBondFirstChange)
            }
            if (hasDiagnostics) {
                OutlinedButton(onClick = onShowDiagnostics, modifier = Modifier.fillMaxWidth()) {
                    Text("查看连接诊断 / 复制")
                }
            }
            results.forEach { dev ->
                AssistChip(
                    onClick = { onPick(dev) },
                    label = { Text("${dev.displayName}") },
                )
            }
        }
    }
}

@Composable
private fun Bubble(msg: UiMessage) {
    val alignment = if (msg.outgoing) Alignment.End else Alignment.Start
    val container = if (msg.outgoing) {
        MaterialTheme.colorScheme.primaryContainer
    } else {
        MaterialTheme.colorScheme.surfaceVariant
    }
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (msg.outgoing) Arrangement.End else Arrangement.Start,
    ) {
        Column(horizontalAlignment = alignment) {
            Text(
                text = "${if (msg.outgoing) "我" else msg.peerName ?: "Host"}  ${timeOf(msg.createdAt)}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                text = msg.text,
                style = MaterialTheme.typography.bodyMedium,
                fontSize = 15.sp,
                modifier = Modifier
                    .background(container, RoundedCornerShape(12.dp))
                    .clickable { }
                    .padding(horizontal = 12.dp, vertical = 8.dp),
            )
        }
    }
}

private fun timeOf(epochMs: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(epochMs))
