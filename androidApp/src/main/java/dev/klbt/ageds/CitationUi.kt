package dev.klbt.ageds

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import dev.klbt.ageds.core.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
fun ColumnScope.CitationPane(artifact: ArtifactSummary, workspace: CitationWorkspace, onBack: () -> Unit) {
    val transcript = workspace.transcript.value
    var wordMode by remember(artifact.id, transcript?.id) { mutableStateOf(false) }
    var anchor by remember(artifact.id, transcript?.id, wordMode) { mutableStateOf<Int?>(null) }
    var endpoint by remember(artifact.id, transcript?.id, wordMode) { mutableStateOf<Int?>(null) }
    var note by remember(artifact.id, transcript?.id) { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(workspace, lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_PAUSE || event == Lifecycle.Event.ON_STOP || event == Lifecycle.Event.ON_DESTROY) workspace.stopAudio()
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); workspace.deactivate() }
    }
    LaunchedEffect(workspace) {
        if (workspace.transcript.value == null && !workspace.busy.value && !workspace.versionPages.loading.value) {
            if (workspace.versions.isEmpty()) workspace.loadOlderVersions() else workspace.load(null)
        }
        while (true) { workspace.tickAudio(); delay(100) } }
    val wordDisplay = remember(transcript) { CitationDisplayProjection.words(transcript) }
    val flatWords = wordDisplay.words
    fun pick(index: Int) {
        val first = anchor
        if (first == null || endpoint != null) { anchor = index; endpoint = null }
        else endpoint = index
        val start = minOf(anchor ?: index, endpoint ?: index)
        val end = maxOf(anchor ?: index, endpoint ?: index)
        if (wordMode) workspace.select(emptyList(), flatWords.subList(start, end + 1).map { it.first })
        else workspace.select((start..end).toList())
    }
    Row {
        TextButton(onClick = onBack) { Text("← Lista") }
        Text(artifact.originalName, modifier = Modifier.weight(1f), fontWeight = FontWeight.Bold)
        TextButton(onClick = workspace::refresh, enabled = !workspace.busy.value) { Text("Odśwież") }
    }
    workspace.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    workspace.message.value?.let { Text(it) }
    if (workspace.busy.value) LinearProgressIndicator(Modifier.fillMaxWidth())
    LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        item {
            Text("Wersje transkrypcji", fontWeight = FontWeight.Bold)
            CitationPageControls(workspace.versionPages, workspace::loadOlderVersions)
        }
        items(workspace.versions, key = { "version-${it.id}" }) { version ->
            TextButton(onClick = { workspace.load(version.id) }) {
                Text("${if (transcript?.id == version.id) "✓ " else ""}#${version.id} · ${version.model ?: "model nieznany"} · ${version.createdAt ?: "czas nieznany"}")
            }
        }
        if (transcript == null) item { Text("Brak wyświetlonej transkrypcji. Stan artefaktu: ${artifact.transcriptStatus}.") }
        else {
            item {
                Text("Wyświetlona wersja #${transcript.id} · ${transcript.language ?: "język nieznany"}")
                Text("Dotknij początku, potem końca ciągłego zakresu. Trzecie dotknięcie zaczyna nowy wybór. Tekst ASR pozostaje bez korekty.", style = MaterialTheme.typography.bodySmall)
                Row {
                    FilterChip(selected = !wordMode, onClick = { wordMode = false; workspace.clearSelection() }, label = { Text("Segmenty") })
                    Spacer(Modifier.width(8.dp))
                    FilterChip(selected = wordMode, onClick = { wordMode = true; workspace.clearSelection() }, label = { Text("Słowa ASR") })
                }
                if (wordMode) {
                    Text("Wybór słów wymaga zapisanych, zgodnych znaczników ASR. Odrzucony zakres nie może być zapisany.", style = MaterialTheme.typography.bodySmall)
                    if (wordDisplay.truncated) Text("Widoczny tylko początek: ${flatWords.size} słów. Dalsze słowa pominięto w tym widoku; można wybrać ich segmenty. To nie jest pełne pokrycie transkrypcji.", style = MaterialTheme.typography.bodySmall)
                } else Text("Jeden cytat obejmuje najwyżej 10000 kolejnych segmentów.", style = MaterialTheme.typography.bodySmall)
            }
            if (transcript.segments.isEmpty()) item { Text(transcript.text) }
            if (wordMode) {
                if (flatWords.isEmpty()) item { Text("Brak zapisanych słów; wybierz segmenty.") }
                itemsIndexed(flatWords) { index, pair ->
                    val selected = anchor != null && index in minOf(anchor!!, endpoint ?: anchor!!)..maxOf(anchor!!, endpoint ?: anchor!!)
                    Text("${if (selected) "☑" else "□"} [${pair.first.segmentIndex}:${pair.first.wordIndex}] ${pair.second}", modifier = Modifier.fillMaxWidth().clickable { pick(index) }.padding(8.dp))
                }
            } else itemsIndexed(transcript.segments) { index, segment ->
                val selected = anchor != null && index in minOf(anchor!!, endpoint ?: anchor!!)..maxOf(anchor!!, endpoint ?: anchor!!)
                Text("${if (selected) "☑" else "□"} [$index] ${segment.start}–${segment.end} s · ${segment.text}", modifier = Modifier.fillMaxWidth().clickable { pick(index) }.padding(8.dp))
            }
        }
        workspace.preview.value?.let { preview -> item {
            HorizontalDivider()
            Text("Dokładny podgląd · wersja #${preview.derivedTextId} · ${preview.startMs}–${preview.endMs} ms")
            Text(preview.quoteText)
            Text("Zgodność tekstu z ASR nie potwierdza alignmentu, odsłuchu ani prawdy wypowiedzi.", style = MaterialTheme.typography.bodySmall)
            Row {
                Button(onClick = workspace::save, enabled = !workspace.busy.value) { Text("Zapisz cytat") }
                TextButton(onClick = { anchor = null; endpoint = null; workspace.clearSelection() }) { Text("Wyczyść wybór") }
            }
        } }
        item {
            HorizontalDivider(); Text("Zapisane cytaty — wszystkie wersje", fontWeight = FontWeight.Bold)
            CitationPageControls(workspace.citationPages, workspace::loadOlderCitations)
            Text("Odsłuch jest przybliżony (seek odtwarzacza / granice ASR), bez potwierdzenia alignmentu.", style = MaterialTheme.typography.bodySmall)
            Text(workspace.audioStatus.value)
            TextButton(onClick = workspace::stopAudio) { Text("Zatrzymaj audio") }
        }
        items(workspace.citations, key = { "citation-${it.id}" }) { citation ->
            Text("#${citation.id} · wersja #${citation.derivedTextId} · ${citation.startMs}–${citation.endMs} ms")
            Text(citation.quoteText)
            TextButton(onClick = { workspace.play(citation) }) { Text("Odtwórz zapisany zakres") }
        }
        item {
            HorizontalDivider(); Text("Oddzielne adnotacje", fontWeight = FontWeight.Bold)
            CitationPageControls(workspace.annotationPages, workspace::loadOlderAnnotations)
        }
        items(workspace.annotations, key = { "note-${it.id}" }) { annotation ->
            Text(annotation.body)
            Text("Wersja #${annotation.derivedTextId ?: "nieprzypięta"} · ${annotation.startMs ?: "—"}–${annotation.endMs ?: "—"} ms", style = MaterialTheme.typography.bodySmall)
        }
        item {
            OutlinedTextField(note, { note = it }, label = { Text("Adnotacja do wyświetlonej wersji / zakresu") }, enabled = !workspace.busy.value, modifier = Modifier.fillMaxWidth())
            Button(onClick = { scope.launch { if (workspace.addNote(note)) note = "" } }, enabled = !workspace.busy.value && note.isNotBlank()) { Text("Zapisz adnotację") }
        }
    }
}

@Composable
private fun <T> CitationPageControls(pages: CitationPages<T>, onMore: () -> Unit) {
    Text(pages.coverage.value, style = MaterialTheme.typography.bodySmall)
    pages.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
    if (pages.loading.value) Text("Wczytywanie strony…", style = MaterialTheme.typography.bodySmall)
    TextButton(onClick = onMore, enabled = pages.canLoadMore.value && !pages.loading.value) {
        Text(if (pages.items.isEmpty()) "Wczytaj listę" else "Wczytaj starsze")
    }
}
