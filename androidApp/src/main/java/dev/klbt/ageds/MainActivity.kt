package dev.klbt.ageds

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MaterialTheme { EvidenceApp() } }
    }
}

class EvidenceVm : ViewModel() {
    val serverUrl = mutableStateOf("http://10.0.2.2:8080")
    val items = mutableStateListOf<ArtifactSummary>()
    val busy = mutableStateOf(false)
    val error = mutableStateOf<String?>(null)
    val selected = mutableStateOf<ArtifactSummary?>(null)
    val transcript = mutableStateOf<Transcript?>(null)
    val annotations = mutableStateListOf<EvidenceAnnotation>()
    private val api get() = EvidenceApi(serverUrl.value)

    suspend fun refresh() {
        busy.value = true
        try { items.clear(); items.addAll(api.artifacts()); error.value = null }
        catch (t: Throwable) { error.value = t.message }
        finally { busy.value = false }
    }

    suspend fun uploadAndQueue(activity: ComponentActivity, uris: List<android.net.Uri>) {
        busy.value = true
        try {
            for ((index, uri) in uris.withIndex()) {
                val uploaded = withContext(Dispatchers.IO) { api.uploadAudio(activity.contentResolver, uri) }
                // Newly selected files are explicitly user-prioritized; earlier selections get slightly higher queue priority.
                val p = TranscriptionPriority.score(PrioritySignals(manualPriority = (100 - index).coerceAtLeast(1)))
                api.queueTranscription(uploaded.artifactId, p)
            }
            refresh()
        } catch (t: Throwable) { error.value = t.message }
        finally { busy.value = false }
    }

    suspend fun open(item: ArtifactSummary) {
        selected.value = item
        transcript.value = try { api.transcript(item.id) } catch (_: Throwable) { null }
        annotations.clear()
        try { annotations.addAll(api.annotations(item.id)) } catch (_: Throwable) {}
    }

    suspend fun addNote(body: String, startMs: Long? = null, endMs: Long? = null) {
        val a = selected.value ?: return
        val saved = api.annotate(a.id, AnnotationCreate(body = body, startMs = startMs, endMs = endMs))
        annotations.add(0, saved)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun EvidenceApp(vm: EvidenceVm = viewModel()) {
    val scope = rememberCoroutineScope()
    val activity = androidx.compose.ui.platform.LocalContext.current as ComponentActivity
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) scope.launch { vm.uploadAndQueue(activity, uris) }
    }
    LaunchedEffect(Unit) { vm.refresh() }

    Scaffold(topBar = {
        TopAppBar(title = { Text("AGEDS · Evidence") }, actions = {
            TextButton(onClick = { picker.launch(arrayOf("audio/*", "video/*")) }) { Text("Import + transcribe") }
            TextButton(onClick = { scope.launch { vm.refresh() } }) { Text("Refresh") }
        })
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(12.dp)) {
            OutlinedTextField(
                value = vm.serverUrl.value,
                onValueChange = { vm.serverUrl.value = it },
                label = { Text("Evidence server") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            vm.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (vm.busy.value) LinearProgressIndicator(Modifier.fillMaxWidth())
            Spacer(Modifier.height(8.dp))
            val selected = vm.selected.value
            if (selected == null) ArtifactList(vm.items) { scope.launch { vm.open(it) } }
            else TranscriptPane(selected, vm.transcript.value, vm.annotations, onBack = { vm.selected.value = null }, onNote = { body -> scope.launch { vm.addNote(body) } })
        }
    }
}

@Composable
private fun ArtifactList(items: List<ArtifactSummary>, onOpen: (ArtifactSummary) -> Unit) {
    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        items(items, key = { it.id }) { a ->
            Card(Modifier.fillMaxWidth().clickable { onOpen(a) }) {
                Column(Modifier.padding(12.dp)) {
                    Text(a.originalName, fontWeight = FontWeight.SemiBold)
                    Text("#${a.id} · ${a.transcriptStatus} · ${a.mimeType ?: "?"}", style = MaterialTheme.typography.bodySmall)
                    a.transcriptPreview?.takeIf { it.isNotBlank() }?.let { Text(it, maxLines = 3) }
                }
            }
        }
    }
}

@Composable
private fun ColumnScope.TranscriptPane(
    artifact: ArtifactSummary,
    transcript: Transcript?,
    annotations: List<EvidenceAnnotation>,
    onBack: () -> Unit,
    onNote: (String) -> Unit,
) {
    var note by remember { mutableStateOf("") }
    Row(verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = onBack) { Text("← list") }
        Text(artifact.originalName, fontWeight = FontWeight.Bold)
    }
    Spacer(Modifier.height(8.dp))
    if (transcript == null) Text("Transcript is not ready yet.") else {
        Text("${transcript.model ?: "transcript"} · ${transcript.language ?: "?"}", style = MaterialTheme.typography.labelMedium)
        Spacer(Modifier.height(6.dp))
        LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            if (transcript.segments.isEmpty()) item { Text(transcript.text) }
            else items(transcript.segments) { s ->
                Text("${"%.1f".format(s.start)}–${"%.1f".format(s.end)}  ${s.text}")
            }
            if (annotations.isNotEmpty()) {
                item { HorizontalDivider(); Text("Annotations", fontWeight = FontWeight.Bold) }
                items(annotations) { a -> Text("${a.label ?: a.kind}: ${a.body}") }
            }
        }
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(note, { note = it }, label = { Text("Annotation") }, modifier = Modifier.weight(1f))
        Button(onClick = { if (note.isNotBlank()) { onNote(note); note = "" } }, modifier = Modifier.padding(start = 8.dp)) { Text("Add") }
    }
}
