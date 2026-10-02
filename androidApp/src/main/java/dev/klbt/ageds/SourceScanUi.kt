package dev.klbt.ageds

import androidx.activity.ComponentActivity
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.klbt.ageds.core.ScanIssue
import dev.klbt.ageds.core.ScannedSourceFile

private fun coverageLabel(value: String) = when (value) {
    "complete_within_scope" -> "Odczytano w zadeklarowanym zakresie"
    "partial" -> "NIEKOMPLETNY ODCZYT — sprawdź ograniczenia"
    "inventory_only" -> "Tylko inwentaryzacja; bez interpretacji treści"
    "unsupported" -> "Format nieobsługiwany; treści nie odczytano"
    "unreadable" -> "Nie udało się odczytać"
    else -> "Nieznany zakres: $value"
}

@Composable
internal fun SourceScanWorkspace(vm: CorpusVm, activity: ComponentActivity, pickFolder: () -> Unit) {
    var query by remember { mutableStateOf("") }
    var audioOnly by remember { mutableStateOf(false) }
    var detailUri by remember { mutableStateOf<String?>(null) }
    var showIssues by remember { mutableStateOf(false) }
    val scan = vm.sourceScan.value
    // A new scan cannot leave a detail dialog or selection attached to a previous source.
    LaunchedEffect(scan?.rootUri, scan?.scannedAt) { detailUri = null; showIssues = false }
    val selected = vm.selectedSourceCandidates()
    val duplicateNames = remember(scan) { scan?.files.orEmpty().groupBy { it.name }.filterValues { it.size > 1 } }
    scan?.files?.firstOrNull { it.uri == detailUri }?.let { file ->
        SourceFileDialog(file, duplicateNames[file.name].orEmpty(), onDismiss = { detailUri = null })
    }
    if (showIssues && scan != null) AlertDialog(
        onDismissRequest = { showIssues = false }, title = { Text("Ograniczenia skanu") },
        text = { LazyColumn(Modifier.heightIn(max = 450.dp)) {
            items(scan.issues) { ScanIssueRow(it) }
            scan.files.filter { it.issues.isNotEmpty() }.forEach { file ->
                item { Text(file.relativePath, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 12.dp)) }
                items(file.issues) { ScanIssueRow(it) }
            }
        } },
        confirmButton = { TextButton(onClick = { showIssues = false }) { Text("Zamknij") } }
    )

    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Poznaj materiał źródłowy", style = MaterialTheme.typography.titleLarge)
            Text("Wskaż folder: pliki i surowe komórki pojawią się bez przygotowanego katalogu. Skan odczytuje źródła; nie zmienia ich i niczego nie wysyła.")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = pickFolder, enabled = !vm.busy.value) { Text("Wskaż folder") }
                if (vm.sourceScanning.value) OutlinedButton(onClick = vm::cancelSourceScan) { Text("Anuluj skan") }
            }
            Text("CSV, TSV i XLSX: rekordy źródłowe. XLS: ograniczony odczyt — sprawdź pokrycie każdego pliku. Audio: inwentaryzacja i odczytywalne metadane.", style = MaterialTheme.typography.bodySmall)
            Text("Tekst: UTF-8 lub UTF-16 ze znacznikiem BOM. Separator CSV jest hipotezą z próbki; niejednoznaczność pozostaje widoczna w szczegółach. TSV używa tabulatora.", style = MaterialTheme.typography.labelSmall)
            vm.sourceAccessNotice.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            vm.sourceCacheNotice.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        } } }

        if (scan != null) {
            item { Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(coverageLabel(scan.coverage), fontWeight = FontWeight.Bold,
                    color = if (scan.coverage == "partial") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
                Text("${scan.files.size} plików · ${scan.scannedDirectories} folderów · odczytano ${scan.bytesRead} B")
                Text("Skan: ${scan.scannedAt}", style = MaterialTheme.typography.bodySmall)
                Text("Folder: ${scan.rootUri}", style = MaterialTheme.typography.labelSmall)
                if (vm.sourceScanCached.value) Text("Zapisany wcześniejszy skan. Dostępność URI i zgodność obecnych bajtów nie zostały ponownie sprawdzone.", color = MaterialTheme.colorScheme.error)
                Text("Nie jest to niezmienna kopia folderu: dostawca może zmieniać zawartość w trakcie lub po skanie. Nazwy, numery i daty nie potwierdzają tożsamości ani zdarzeń.", style = MaterialTheme.typography.bodySmall)
                val limits = scan.limits
                Text("Limity: ${limits.maxFiles} plików, ${limits.maxDirectories} folderów, głębokość ${limits.maxDepth}; ${limits.maxFileBytes} B/plik, ${limits.maxTotalBytes} B razem; ${limits.maxRowsPerFile} wierszy i ${limits.maxCellsPerFile} komórek/plik. Cache ≤4 MiB.", style = MaterialTheme.typography.labelSmall)
                val issueCount = scan.issues.size + scan.files.sumOf { it.issues.size }
                if (issueCount > 0) TextButton(onClick = { showIssues = true }) { Text("Zobacz ograniczenia i błędy ($issueCount)") }
                if (duplicateNames.isNotEmpty()) Text("Kolizje nazw: ${duplicateNames.size}. Zachowano wszystkie URI; szczegóły pliku pokazują pozostałe lokalizacje.", fontWeight = FontWeight.SemiBold)
                Text("Powiązania nagrań z numerami, datami i wierszami nie są automatycznie ustalane.", style = MaterialTheme.typography.bodySmall)
            } } }
            item { Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Nagrania do następnego kroku: ${selected.size}", fontWeight = FontWeight.Bold)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    TextButton(onClick = vm::selectSourceAudio) { Text("Zaznacz audio") }
                    TextButton(onClick = vm::clearSourceSelection) { Text("Wyczyść wybór") }
                }
                Button(onClick = { vm.prepareTranscription(activity, selected) }, enabled = selected.isNotEmpty() && !vm.busy.value) { Text("Przygotuj transkrypcję (${selected.size})") }
                Text("Na kolejnym ekranie przejrzysz pliki i wybierzesz serwer. Wysłanie wymaga naciśnięcia przycisku wysyłania.", style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(query, { query = it }, label = { Text("Szukaj nazwy lub ścieżki") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
                FilterChip(audioOnly, { audioOnly = !audioOnly }, label = { Text("Tylko audio") })
            } }
            val visible = scan.files.filter { file -> (!audioOnly || file.kind == "audio") &&
                (query.isBlank() || file.relativePath.contains(query, true) || file.name.contains(query, true)) }
            if (visible.isEmpty()) item { Text("Brak plików spełniających filtr.") }
            items(visible, key = { it.uri }) { file ->
                Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(10.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        if (file.kind == "audio") Checkbox(file.uri in vm.selectedSourceUris, { vm.toggleSource(file.uri) })
                        Column(Modifier.weight(1f).clickable { detailUri = file.uri }) {
                            Text(file.name, fontWeight = FontWeight.SemiBold)
                            Text(file.relativePath, style = MaterialTheme.typography.bodySmall, maxLines = 3, overflow = TextOverflow.Ellipsis)
                            Text("${file.sizeBytes?.let { "$it B" } ?: "Rozmiar nieznany"} · ${coverageLabel(file.coverage)}", style = MaterialTheme.typography.labelSmall)
                            SourceAudioProjection.display(file)?.let { Text(it.summary, style = MaterialTheme.typography.labelSmall) }
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = { detailUri = file.uri }) { Text("Źródło i rekordy (${file.rows.size})") }
                        if (file.kind == "audio") TextButton(onClick = { vm.openCandidate(activity, vm.sourceCandidate(file), file.mime) }) { Text("Odsłuch") }
                    }
                } }
            }
        }
    }
}

@Composable
private fun ScanIssueRow(issue: ScanIssue) {
    Column(Modifier.padding(vertical = 5.dp)) {
        Text("${issue.code}: ${issue.message}")
        issue.locator?.let { Text(it, style = MaterialTheme.typography.labelSmall) }
    }
}

@Composable
private fun SourceFileDialog(file: ScannedSourceFile, collisions: List<ScannedSourceFile>, onDismiss: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text(file.name) }, text = {
        SelectionContainer { LazyColumn(Modifier.heightIn(max = 500.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item {
                Text("${file.relativePath}\nURI: ${file.uri}\n${coverageLabel(file.coverage)}")
                val audioDisplay = SourceAudioProjection.display(file)
                if (audioDisplay != null) {
                    Text(audioDisplay.summary, fontWeight = FontWeight.SemiBold)
                    audioDisplay.details.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
                } else {
                    Text("SHA-256: ${file.sha256 ?: "nie obliczono pełnego hash"}", style = MaterialTheme.typography.bodySmall)
                    file.audioDurationSec?.let { Text("Długość z nagłówka audio: $it s") }
                }
                Text("Hash porównuje odczytane bajty; nie potwierdza ich autorstwa ani prawdziwości.", style = MaterialTheme.typography.labelSmall)
            }
            file.textFormat?.let { format ->
                item {
                    Text("Odczyt tekstu", fontWeight = FontWeight.SemiBold)
                    val encodingBasis = if (format.encodingBasis == "bom") "znacznik BOM źródła" else "domyślna interpretacja UTF-8, bez BOM"
                    Text("Kodowanie: ${format.encoding} ($encodingBasis)", style = MaterialTheme.typography.bodySmall)
                    val basis = when (format.delimiterBasis) {
                        "tsv_extension" -> "wybrany na podstawie rozszerzenia TSV"
                        "inferred_uniform_records" -> "wywnioskowany z próbki, niepotwierdzony przez autora źródła"
                        else -> "prowizoryczny wybór domyślny"
                    }
                    Text("Separator: ${separatorLabel(format.delimiter)} — $basis", style = MaterialTheme.typography.bodySmall)
                    Text("Próbka: ${format.sampledRecords} rekordów${if (format.sampleTruncated) "; ograniczona limitem" else ""}.", style = MaterialTheme.typography.labelSmall)
                    if (format.delimiterAmbiguous) Text("Separator niejednoznaczny. Podział na komórki wymaga sprawdzenia wobec źródła.", color = MaterialTheme.colorScheme.error)
                    if (format.delimiterCandidates.isNotEmpty()) Text("Kandydaci: ${format.delimiterCandidates.joinToString { separatorLabel(it) }}", style = MaterialTheme.typography.labelSmall)
                }
            }
            if (collisions.size > 1) {
                item { Text("Ta sama nazwa w różnych lokalizacjach:", fontWeight = FontWeight.Bold) }
                items(collisions) { Text("${it.relativePath}\n${it.uri}", style = MaterialTheme.typography.bodySmall) }
            }
            items(file.issues) { ScanIssueRow(it) }
            item { Text("Surowe rekordy", fontWeight = FontWeight.Bold)
                Text("Raw zachowuje reprezentację komórki: token CSV, wartość zapisaną w XLSX lub bajty rekordu XLS w zapisie szesnastkowym. Wartość jest projekcją parsera. Formuły nie są wykonywane. Rekordy nie zastępują oryginalnego pliku.", style = MaterialTheme.typography.bodySmall) }
            file.rows.forEach { row ->
                item { Text(row.locator, fontWeight = FontWeight.SemiBold) }
                items(row.cells) { cell ->
                    Column {
                        Text("Kolumna ${cell.column} · raw: ${cell.raw}")
                        cell.sourceReference?.let { Text("Adres źródłowy: $it", style = MaterialTheme.typography.labelSmall) }
                        cell.sourceType?.let { Text("Typ źródłowy: $it", style = MaterialTheme.typography.labelSmall) }
                        Text("Wartość: ${cell.value}", style = MaterialTheme.typography.bodySmall)
                        cell.formula?.let { Text("Formuła (niewykonana): $it", style = MaterialTheme.typography.bodySmall) }
                    }
                }
            }
            if (file.rows.isEmpty()) item { Text("Brak odczytanych rekordów tabelarycznych. Sprawdź format i ograniczenia powyżej.") }
        } }
    }, confirmButton = { TextButton(onClick = onDismiss) { Text("Zamknij") } })
}

private fun separatorLabel(value: String) = when (value) {
    "," -> "przecinek"
    ";" -> "średnik"
    "\t" -> "tabulator"
    "|" -> "pionowa kreska |"
    else -> value
}
