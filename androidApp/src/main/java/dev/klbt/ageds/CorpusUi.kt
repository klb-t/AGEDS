package dev.klbt.ageds

import android.content.Intent
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.klbt.ageds.core.*

private enum class CorpusSection(val title: String) { NUMBERS("Numery"), SMS("SMS"), CALLS("Połączenia"), RECORDINGS("Nagrania"), EMAILS("E-maile") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CorpusApp(vm: CorpusVm = viewModel()) {
    val activity = androidx.compose.ui.platform.LocalContext.current as ComponentActivity
    val importPack = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) vm.importCorpus(uri)
    }
    val folderPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri != null) {
            runCatching { activity.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            vm.indexRecordings(uri)
        }
    }
    Scaffold(topBar = { TopAppBar(title = { Text("AGEDS · Korpus komunikacji") }, actions = {
        TextButton(onClick = { activity.startActivity(Intent(activity, MainActivity::class.java)) }) { Text("Transkrypcje") }
    }) }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            vm.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(horizontal = 12.dp)) }
            vm.message.value?.let { Text(it, modifier = Modifier.padding(horizontal = 12.dp), style = MaterialTheme.typography.bodySmall) }
            vm.progress.value?.let { Text(it, modifier = Modifier.padding(horizontal = 12.dp), fontWeight = FontWeight.Bold) }
            if (vm.busy.value) LinearProgressIndicator(Modifier.fillMaxWidth())
            val seed = vm.corpus.value
            if (seed == null) {
                Card(Modifier.padding(16.dp).fillMaxWidth()) { Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("Wczytaj prywatny katalog", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                    Text("Google Drive → a → ageds-corpus-seed.json. Po imporcie katalog działa offline.")
                    Button(onClick = { importPack.launch(arrayOf("application/json", "text/plain", "application/octet-stream")) }) { Text("Wczytaj pakiet") }
                } }
            } else CorpusLoaded(
                vm, seed, activity,
                importPack = { importPack.launch(arrayOf("application/json", "text/plain", "application/octet-stream")) },
                pickRecordings = { folderPicker.launch(null) }
            )
        }
    }
}

@Composable
private fun ColumnScope.CorpusLoaded(
    vm: CorpusVm,
    seed: CorpusSeed,
    activity: ComponentActivity,
    importPack: () -> Unit,
    pickRecordings: () -> Unit
) {
    var section by remember { mutableStateOf(CorpusSection.NUMBERS) }
    var query by remember { mutableStateOf("") }
    var candidateChoice by remember { mutableStateOf<CorpusRecording?>(null) }
    var chooseForTranscription by remember { mutableStateOf(false) }
    val sms = vm.selectedSms()
    val calls = vm.selectedCalls()
    val recordings = vm.selectedRecordings()
    val emails = vm.selectedEmails()
    val linked = vm.linkedSelectedCount()
    val selectedCandidates = vm.selectedCandidates()

    candidateChoice?.let { recording ->
        AlertDialog(
            onDismissRequest = { candidateChoice = null },
            title = { Text("Wybierz plik: ${recording.name}") },
            text = { Column {
                Text("Ta sama nazwa występuje w kilku lokalizacjach. Nazwa nie potwierdza tożsamości ani rozmówcy.")
                LazyColumn(Modifier.heightIn(max = 340.dp)) {
                    items(vm.candidates(recording), key = { it.uri.toString() }) { candidate ->
                        TextButton(onClick = {
                            if (chooseForTranscription) vm.prepareTranscription(activity, listOf(candidate))
                            else vm.openCandidate(activity, candidate, recording.mime)
                            candidateChoice = null
                        }) { Column(Modifier.fillMaxWidth()) {
                            Text(candidate.relativePath)
                            Text("${candidate.sizeBytes} B · ${candidate.uri}", style = MaterialTheme.typography.labelSmall)
                        } }
                    }
                }
            } },
            confirmButton = { TextButton(onClick = { candidateChoice = null }) { Text("Zamknij") } }
        )
    }

    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("${seed.contacts.size} numerów · ${seed.sms.size} SMS · ${seed.calls.size} połączeń · ${seed.recordings.size} nagrań", fontWeight = FontWeight.Bold)
            Text("Wybrane: ${vm.selectedPhones().size} numerów · ${sms.size} SMS/MMS · ${calls.size} połączeń · ${recordings.size} nagrań · ${emails.size} e-maili", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = vm::selectPriority) { Text("Priorytetowe") }
                OutlinedButton(onClick = vm::clearSelection) { Text("Wyczyść") }
                TextButton(onClick = importPack) { Text("Zmień pakiet") }
            }
        } } }

        item {
            Text("Grupy", fontWeight = FontWeight.Bold)
            seed.presets.filter { it.id != "priority_institutions" }.forEach { p ->
                Row(Modifier.fillMaxWidth().clickable { vm.togglePreset(p.id) }.padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(p.id in vm.selectedPresetIds, { vm.togglePreset(p.id) })
                    Column { Text(p.label, fontWeight = FontWeight.SemiBold); Text(p.description, style = MaterialTheme.typography.bodySmall) }
                }
            }
        }

        item { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Nagrania", fontWeight = FontWeight.Bold)
            Text(
                "Nazwy z kandydatami w wybranym folderze: ${vm.linkedRecordingCandidates.size}; z aktualnego katalogu $linked/${recordings.size} pozycji. " +
                    "Wszystkie pliki o tej samej nazwie są zachowane. Powiązania katalogu, numery i daty są danymi źródłowymi do weryfikacji.",
                style = MaterialTheme.typography.bodySmall
            )
            if (vm.scanFiles.value > 0) {
                Text("Ostatni skan: ${vm.scanFiles.value} plików · ${vm.scanDirectories.value} folderów.", style = MaterialTheme.typography.bodySmall)
                if (vm.scanSamples.value.isNotEmpty()) Text("Przykłady: ${vm.scanSamples.value.take(4).joinToString()}", style = MaterialTheme.typography.labelSmall)
            }
            vm.indexedTree.value?.let { Text("Źródło indeksu: $it", style = MaterialTheme.typography.labelSmall) }
            OutlinedButton(onClick = pickRecordings, enabled = !vm.busy.value) { Text("Wskaż folder z nagraniami") }
            OutlinedButton(
                onClick = { vm.prepareTranscription(activity, selectedCandidates) },
                enabled = !vm.busy.value && selectedCandidates.isNotEmpty()
            ) { Text("Przygotuj transkrypcję (${selectedCandidates.size} plików)") }
            Text("Następny krok pozwala przejrzeć pliki i wybrać serwer przed wysłaniem. Oryginały są tylko odczytywane.", style = MaterialTheme.typography.bodySmall)
        } } }

        item {
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                CorpusSection.entries.forEach { s -> FilterChip(section == s, { section = s }, label = { Text(s.title) }) }
            }
            OutlinedTextField(query, { query = it }, label = { Text("Szukaj") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        }

        when (section) {
            CorpusSection.NUMBERS -> items(
                seed.contacts.filter { c -> query.isBlank() || listOf(c.phone, c.label, c.backupName, c.publicId).any { it?.contains(query, true) == true } }
                    .sortedByDescending { it.interactions }
            ) { c ->
                Row(Modifier.fillMaxWidth().clickable { vm.togglePhone(c.phone) }.padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(vm.isPhoneSelected(c.phone), { vm.togglePhone(c.phone) })
                    Column(Modifier.weight(1f)) {
                        Text(c.label ?: c.phone ?: "?", fontWeight = FontWeight.SemiBold)
                        Text("${c.phone ?: ""} · ${c.calls} calls · ${c.sms} SMS · ${c.recordings} nagrań", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
            CorpusSection.SMS -> items(sms.filter { query.isBlank() || it.text.contains(query, true) || it.contact?.contains(query, true) == true || it.rawSender?.contains(query, true) == true }) { s ->
                CorpusRow("${s.contact ?: vm.labelFor(s.phone)} · ${s.at ?: ""}", "${s.type ?: ""} · ${s.rawSender ?: s.phone ?: ""}", s.text)
            }
            CorpusSection.CALLS -> items(calls.filter { query.isBlank() || it.contact?.contains(query, true) == true || it.phone?.contains(query, true) == true }) { c ->
                CorpusRow("${c.contact ?: vm.labelFor(c.phone)} · ${c.start ?: ""}", "${c.type ?: ""} · ${c.durationSec}s · ${c.phone ?: ""}")
            }
            CorpusSection.RECORDINGS -> items(
                recordings.filter { query.isBlank() || it.name.contains(query, true) || it.contact?.contains(query, true) == true || it.phone?.contains(query, true) == true }
            ) { r ->
                val candidates = vm.candidates(r)
                Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(10.dp)) {
                    Text("${r.contact ?: vm.labelFor(r.phone)} · ${r.resolvedTime ?: r.callStart ?: ""}", fontWeight = FontWeight.SemiBold)
                    Text("${r.name} · ${candidates.size} kandydatów · pewność w katalogu: ${r.confidence ?: "?"}", style = MaterialTheme.typography.bodySmall)
                    r.folder?.let { Text("Folder katalogu: $it", style = MaterialTheme.typography.labelSmall) }
                    r.note?.takeIf { it.isNotBlank() }?.let { Text(it, maxLines = 5, overflow = TextOverflow.Ellipsis) }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = {
                            if (candidates.size > 1) { candidateChoice = r; chooseForTranscription = false }
                            else vm.openRecording(activity, r)
                        }) { Text(if (candidates.isEmpty()) "Otwórz Drive" else "Odsłuch") }
                        TextButton(onClick = {
                            if (candidates.size > 1) { candidateChoice = r; chooseForTranscription = true }
                            else vm.prepareTranscription(activity, candidates)
                        }, enabled = candidates.isNotEmpty()) { Text("Transkrypcja") }
                    }
                } }
            }
            CorpusSection.EMAILS -> {
                val domains = vm.emailDomains()
                if (domains.isNotEmpty()) item { Text("Reguły domen: ${domains.joinToString { "@$it" }}", style = MaterialTheme.typography.bodySmall) }
                items(emails.filter { query.isBlank() || it.email.contains(query, true) || it.label?.contains(query, true) == true }) { e ->
                    CorpusRow(e.label ?: e.email, e.email, e.source)
                }
            }
        }
    }
}

@Composable
private fun CorpusRow(title: String, subtitle: String? = null, body: String? = null, onClick: (() -> Unit)? = null) {
    val modifier = if (onClick == null) Modifier.fillMaxWidth() else Modifier.fillMaxWidth().clickable(onClick = onClick)
    Card(modifier) {
        Column(Modifier.padding(10.dp)) {
            Text(title, fontWeight = FontWeight.SemiBold)
            subtitle?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            body?.takeIf { it.isNotBlank() }?.let { Text(it, maxLines = 5, overflow = TextOverflow.Ellipsis) }
        }
    }
}
