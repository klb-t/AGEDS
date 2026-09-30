package dev.klbt.ageds

import android.app.Application
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
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
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.URI

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val selectionId = intent.getStringExtra(PendingRecordingStore.EXTRA_SELECTION)
        setContent { MaterialTheme { EvidenceApp(selectionId) } }
    }
}

data class UploadTarget(val serverUrl: String, val uri: String)

data class UploadOutcome(val artifactId: Long? = null, val jobId: Long? = null, val error: String? = null)

class EvidenceVm(application: Application) : AndroidViewModel(application) {
    private val prefs = application.getSharedPreferences("ageds-evidence", Application.MODE_PRIVATE)
    val serverUrl = mutableStateOf(prefs.getString("serverUrl", null) ?: "http://10.0.2.2:8080")
    val items = mutableStateListOf<ArtifactSummary>()
    val pending = mutableStateListOf<PendingRecording>()
    val pendingSelected = mutableStateMapOf<String, Boolean>()
    val uploadOutcomes = mutableStateMapOf<UploadTarget, UploadOutcome>()
    val busy = mutableStateOf(false)
    val progress = mutableStateOf<String?>(null)
    val error = mutableStateOf<String?>(null)
    val message = mutableStateOf<String?>(null)
    val selected = mutableStateOf<ArtifactSummary?>(null)
    val transcript = mutableStateOf<Transcript?>(null)
    val annotations = mutableStateListOf<EvidenceAnnotation>()
    private var apiUrl: String? = null
    private var cachedApi: EvidenceApi? = null
    private var loadedSelectionId: String? = null

    private fun api(): EvidenceApi {
        val url = serverUrl.value.trim().trimEnd('/')
        val parsed = URI(url)
        require(parsed.scheme in listOf("https", "http") && !parsed.host.isNullOrBlank()) { "Podaj pełny adres serwera HTTP lub HTTPS" }
        prefs.edit().putString("serverUrl", url).apply()
        if (apiUrl != url) {
            cachedApi?.close()
            cachedApi = EvidenceApi(url)
            apiUrl = url
        }
        return requireNotNull(cachedApi)
    }

    override fun onCleared() {
        cachedApi?.close()
        super.onCleared()
    }

    suspend fun loadSelection(activity: ComponentActivity, id: String?): Boolean {
        if (id == null || loadedSelectionId == id) return true
        return try {
            val recordings = withContext(Dispatchers.IO) { PendingRecordingStore(activity).read(id) }
            stage(recordings)
            loadedSelectionId = id
            true
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Nie mogę odczytać wyboru nagrań: ${t.message}"
            false
        }
    }

    private fun stage(recordings: List<PendingRecording>) {
        val existing = pending.map { it.uri }.toHashSet()
        recordings.forEach { recording ->
            if (existing.add(recording.uri)) {
                pending.add(recording)
                pendingSelected[recording.uri] = true
            }
        }
    }

    suspend fun stageUris(activity: ComponentActivity, uris: List<Uri>) {
        try {
            val recordings = withContext(Dispatchers.IO) {
                uris.map { uri ->
                    val name = activity.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                        if (cursor.moveToFirst()) cursor.getString(0) else null
                    } ?: uri.lastPathSegment ?: "nagranie"
                    PendingRecording(uri.toString(), name, uri.toString())
                }
            }
            stage(recordings)
            error.value = null
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Wybór plików: ${t.message}"
        }
    }

    suspend fun refresh() {
        if (busy.value) return
        busy.value = true
        try { fetchArtifacts(api()); error.value = null }
        catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Serwer: ${t.message}"
        } finally { busy.value = false }
    }

    private suspend fun fetchArtifacts(server: EvidenceApi) {
        val fetched = server.artifacts()
        items.clear(); items.addAll(fetched)
    }

    private fun normalizedServerUrl() = serverUrl.value.trim().trimEnd('/')
    fun outcome(recording: PendingRecording) = uploadOutcomes[UploadTarget(normalizedServerUrl(), recording.uri)]
    fun pendingCount() = pending.count { pendingSelected[it.uri] == true && outcome(it)?.jobId == null }

    fun changeServerUrl(value: String) {
        if (value == serverUrl.value) return
        serverUrl.value = value
        items.clear()
        selected.value = null
        transcript.value = null
        annotations.clear()
        message.value = null
    }

    /** Read originals once per upload. Failures stay attached to their exact locator. */
    suspend fun uploadAndQueue(activity: ComponentActivity) {
        if (busy.value) return
        val chosen = pending.filter { pendingSelected[it.uri] == true && outcome(it)?.jobId == null }
        if (chosen.isEmpty()) return
        busy.value = true
        error.value = null
        try {
            val server = api()
            val targetUrl = normalizedServerUrl()
            for ((index, recording) in chosen.withIndex()) {
                val target = UploadTarget(targetUrl, recording.uri)
                progress.value = "${index + 1}/${chosen.size}: ${recording.name}"
                var artifactId = uploadOutcomes[target]?.artifactId
                try {
                    if (artifactId == null) {
                        val uploaded = withContext(Dispatchers.IO) {
                            server.uploadAudio(activity.contentResolver, Uri.parse(recording.uri), sourcePath = recording.locator)
                        }
                        require(uploaded.ok) { "Serwer odrzucił import" }
                        artifactId = uploaded.artifactId
                        uploadOutcomes[target] = UploadOutcome(artifactId = artifactId)
                    }
                    val priority = TranscriptionPriority.score(PrioritySignals(manualPriority = (100 - index).coerceAtLeast(1)))
                    val queued = server.queueTranscription(requireNotNull(artifactId), priority)
                    uploadOutcomes[target] = UploadOutcome(artifactId, queued.jobId)
                } catch (t: Throwable) {
                    if (t is CancellationException) throw t
                    uploadOutcomes[target] = UploadOutcome(artifactId = artifactId, error = t.message ?: t.toString())
                }
            }
            val sent = chosen.count { uploadOutcomes[UploadTarget(targetUrl, it.uri)]?.jobId != null }
            message.value = "W kolejce: $sent/${chosen.size} · błędy: ${chosen.size - sent}. Gotowość transkrypcji pokazuje stan artefaktu na serwerze."
            try { fetchArtifacts(server) } catch (t: Throwable) {
                if (t is CancellationException) throw t
                error.value = "Zadania zostały zgłoszone, ale odświeżenie listy nie powiodło się: ${t.message}"
            }
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Transkrypcja: ${t.message}"
        } finally { busy.value = false; progress.value = null }
    }

    suspend fun open(item: ArtifactSummary) {
        if (busy.value) return
        busy.value = true
        selected.value = item
        transcript.value = null
        annotations.clear()
        try {
            val server = api()
            transcript.value = server.transcript(item.id)
            annotations.addAll(server.annotations(item.id))
            error.value = null
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Odczyt artefaktu: ${t.message}"
        } finally { busy.value = false }
    }

    suspend fun addNote(body: String, startMs: Long? = null, endMs: Long? = null): Boolean {
        val artifact = selected.value ?: return false
        if (busy.value) return false
        busy.value = true
        return try {
            val saved = api().annotate(artifact.id, AnnotationCreate(body = body, startMs = startMs, endMs = endMs, derivedTextId = transcript.value?.id))
            annotations.add(0, saved)
            error.value = null
            true
        } catch (t: Throwable) {
            if (t is CancellationException) throw t
            error.value = "Zapis adnotacji: ${t.message}"
            false
        } finally { busy.value = false }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun EvidenceApp(selectionId: String? = null, vm: EvidenceVm = viewModel()) {
    val scope = rememberCoroutineScope()
    val activity = androidx.compose.ui.platform.LocalContext.current as ComponentActivity
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) scope.launch {
            uris.forEach { uri -> runCatching {
                activity.contentResolver.takePersistableUriPermission(uri, android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
            } }
            vm.stageUris(activity, uris)
        }
    }
    LaunchedEffect(selectionId) {
        if (vm.loadSelection(activity, selectionId)) vm.refresh()
    }

    Scaffold(topBar = {
        TopAppBar(title = { Text("AGEDS · Transkrypcje") }, actions = {
            TextButton(onClick = { picker.launch(arrayOf("audio/*", "video/*")) }, enabled = !vm.busy.value) { Text("Wybierz pliki") }
            TextButton(onClick = { scope.launch { vm.refresh() } }, enabled = !vm.busy.value) { Text("Odśwież") }
        })
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = vm.serverUrl.value,
                onValueChange = vm::changeServerUrl,
                label = { Text("Serwer transkrypcji") },
                singleLine = true,
                enabled = !vm.busy.value,
                modifier = Modifier.fillMaxWidth()
            )
            vm.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            vm.message.value?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            vm.progress.value?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            if (vm.busy.value) LinearProgressIndicator(Modifier.fillMaxWidth())
            val selected = vm.selected.value
            if (selected == null) {
                if (vm.pending.isNotEmpty()) PendingPane(vm) { scope.launch { vm.uploadAndQueue(activity) } }
                ArtifactList(vm.items, Modifier.weight(1f)) { scope.launch { vm.open(it) } }
            } else TranscriptPane(
                selected, vm.transcript.value, vm.annotations, vm.busy.value,
                onBack = { vm.selected.value = null },
                onReload = { scope.launch { vm.open(selected) } },
                onNote = { body, start, end -> vm.addNote(body, start, end) }
            )
        }
    }
}

@Composable
private fun PendingPane(vm: EvidenceVm, onSubmit: () -> Unit) {
    var expanded by remember { mutableStateOf(true) }
    Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        TextButton(onClick = { expanded = !expanded }) { Text("Wybór plików (${vm.pending.size}) ${if (expanded) "▾" else "▸"}") }
        if (expanded) {
            Text("Sprawdź konkretne lokalizacje. Nazwa i dane katalogu nie potwierdzają tożsamości rozmówcy. Wysłanie tworzy kopię na wskazanym serwerze; oryginały pozostają bez zmian.", style = MaterialTheme.typography.bodySmall)
            LazyColumn(Modifier.heightIn(max = 190.dp)) {
                items(vm.pending, key = { it.uri }) { recording ->
                    val outcome = vm.outcome(recording)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(vm.pendingSelected[recording.uri] == true, { vm.pendingSelected[recording.uri] = it }, enabled = !vm.busy.value && outcome?.jobId == null)
                        Column(Modifier.weight(1f)) {
                            Text(recording.name, fontWeight = FontWeight.SemiBold)
                            Text(recording.locator, style = MaterialTheme.typography.labelSmall)
                            if (recording.locator != recording.uri) Text(recording.uri, style = MaterialTheme.typography.labelSmall)
                            outcome?.jobId?.let { Text("W kolejce · zadanie $it · artefakt ${outcome.artifactId}", style = MaterialTheme.typography.bodySmall) }
                            outcome?.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                        }
                    }
                }
            }
        }
        Button(onClick = onSubmit, enabled = !vm.busy.value && vm.pendingCount() > 0) { Text("Wyślij i zleć transkrypcję (${vm.pendingCount()})") }
    } }
}

@Composable
private fun ArtifactList(items: List<ArtifactSummary>, modifier: Modifier, onOpen: (ArtifactSummary) -> Unit) {
    LazyColumn(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (items.isEmpty()) item { Text("Brak odczytanych artefaktów. Wybierz pliki lub odśwież po połączeniu z serwerem.") }
        items(items, key = { it.id }) { artifact ->
            Card(Modifier.fillMaxWidth().clickable { onOpen(artifact) }) {
                Column(Modifier.padding(12.dp)) {
                    Text(artifact.originalName, fontWeight = FontWeight.SemiBold)
                    Text("#${artifact.id} · ${artifact.transcriptStatus} · ${artifact.mimeType ?: "?"}", style = MaterialTheme.typography.bodySmall)
                    artifact.transcriptPreview?.takeIf { it.isNotBlank() }?.let { Text(it, maxLines = 3) }
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
    busy: Boolean,
    onBack: () -> Unit,
    onReload: () -> Unit,
    onNote: suspend (String, Long?, Long?) -> Boolean,
) {
    var note by remember(artifact.id) { mutableStateOf("") }
    var interval by remember(artifact.id, transcript?.id) { mutableStateOf<Pair<Long, Long>?>(null) }
    val scope = rememberCoroutineScope()
    Row(verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = onBack, enabled = !busy) { Text("← Lista") }
        Text(artifact.originalName, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
        TextButton(onClick = onReload, enabled = !busy) { Text("Odśwież") }
    }
    if (transcript == null) Text("Transkrypcja nie jest dostępna. Stan z ostatniej listy: ${artifact.transcriptStatus}.")
    else Text("Wersja #${transcript.id} · ${transcript.model ?: "model nieznany"} · ${transcript.language ?: "język nieznany"}", style = MaterialTheme.typography.labelMedium)
    LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (transcript != null) {
            if (transcript.segments.isEmpty()) item { Text(transcript.text) }
            else items(transcript.segments) { segment ->
                Text("${"%.1f".format(segment.start)}–${"%.1f".format(segment.end)}  ${segment.text}",
                    modifier = Modifier.fillMaxWidth().clickable {
                        interval = Math.rint(segment.start * 1000).toLong() to Math.rint(segment.end * 1000).toLong()
                    }.padding(vertical = 4.dp))
            }
        }
        if (annotations.isNotEmpty()) {
            item { HorizontalDivider(); Text("Adnotacje", fontWeight = FontWeight.Bold) }
            items(annotations) { annotation ->
                Text("${annotation.label ?: annotation.kind}: ${annotation.body}")
                Text("Wersja tekstu: ${annotation.derivedTextId ?: "nieprzypięta"} · zakres: ${annotation.startMs ?: "—"}–${annotation.endMs ?: "—"} ms", style = MaterialTheme.typography.labelSmall)
            }
        }
    }
    interval?.let { range -> Row(verticalAlignment = Alignment.CenterVertically) {
        Text("Zakres adnotacji: ${range.first}–${range.second} ms", modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
        TextButton(onClick = { interval = null }, enabled = !busy) { Text("Bez zakresu") }
    } }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(note, { note = it }, label = { Text("Oddzielna adnotacja") }, enabled = !busy, modifier = Modifier.weight(1f))
        Button(onClick = { scope.launch { if (onNote(note, interval?.first, interval?.second)) note = "" } }, enabled = !busy && note.isNotBlank(), modifier = Modifier.padding(start = 8.dp)) { Text("Zapisz") }
    }
}
