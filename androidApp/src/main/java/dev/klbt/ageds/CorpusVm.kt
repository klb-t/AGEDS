package dev.klbt.ageds

import android.app.Application
import android.content.Intent
import android.net.Uri
import androidx.activity.ComponentActivity
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import dev.klbt.ageds.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive

class CorpusVm(application: Application) : AndroidViewModel(application) {
    val corpus = mutableStateOf<CorpusSeed?>(null)
    val selectedPresetIds = mutableStateListOf<String>()
    val busy = mutableStateOf(false)
    val error = mutableStateOf<String?>(null)
    val message = mutableStateOf<String?>(null)
    val progress = mutableStateOf<String?>(null)
    val linkedRecordingCandidates = mutableStateMapOf<String, List<RecordingCandidate>>()
    val scanFiles = mutableStateOf(0)
    val scanDirectories = mutableStateOf(0)
    val scanSamples = mutableStateOf<List<String>>(emptyList())
    val indexedTree = mutableStateOf<String?>(null)
    val sourceScan = mutableStateOf<SourceScanResult?>(null)
    val sourceScanCached = mutableStateOf(false)
    val sourceCacheNotice = mutableStateOf<String?>(null)
    val sourceAccessNotice = mutableStateOf<String?>(null)
    val selectedSourceUris = mutableStateListOf<String>()
    val sourceScanning = mutableStateOf(false)
    private val sourceCache = SourceScanCache(application)
    private val manualPhones = mutableStateListOf<String>()
    private val excludedPhones = mutableStateListOf<String>()
    private val store = CorpusStore(application)
    private var scanJob: Job? = null
    private val scanPublication = SourceScanPublicationGate()
    private var importing = false
    private var scanning = false
    private fun updateBusy() { busy.value = importing || scanning }

    init {
        importing = true
        updateBusy()
        viewModelScope.launch {
            try {
                val seed = withContext(Dispatchers.IO) { runCatching { store.loadCached() } }
                seed.onSuccess { it?.let(::install) }.onFailure {
                    if (it is CancellationException) throw it
                    error.value = "Poprzedni katalog nie został wczytany: ${it.message}. Możesz nadal skanować źródła."
                }
                val cached = withContext(Dispatchers.IO) { runCatching { sourceCache.read() } }
                cached.onSuccess { scan ->
                    if (scan != null) { installScan(scan); sourceScanCached.value = true }
                }.onFailure {
                    if (it is CancellationException) throw it
                    sourceCacheNotice.value = "Poprzedniego skanu nie można odczytać: ${it.message}"
                }
            } finally { importing = false; updateBusy() }
        }
    }

    private fun install(seed: CorpusSeed) {
        corpus.value = seed
        selectPriority()
    }

    fun importCorpus(uri: Uri) {
        if (busy.value) return
        viewModelScope.launch {
            importing = true
            updateBusy()
            try {
                val seed = withContext(Dispatchers.IO) { store.import(uri) }
                install(seed)
                message.value = "Wczytano ${seed.contacts.size} numerów · ${seed.sms.size} SMS · ${seed.calls.size} połączeń · ${seed.recordings.size} nagrań."
                error.value = null
            } catch (t: Throwable) {
                if (t is CancellationException) throw t
                error.value = "Import: ${t.message}"
            } finally { importing = false; updateBusy() }
        }
    }

    fun selectPriority() {
        selectedPresetIds.clear()
        selectedPresetIds.addAll(corpus.value?.priorityPresetIds().orEmpty())
        manualPhones.clear(); excludedPhones.clear()
    }
    fun clearSelection() { selectedPresetIds.clear(); manualPhones.clear(); excludedPhones.clear() }
    fun togglePreset(id: String) { if (id in selectedPresetIds) selectedPresetIds.remove(id) else selectedPresetIds.add(id) }

    private fun activePresets() = corpus.value?.presets.orEmpty().filter { it.id in selectedPresetIds }
    private fun presetPhones() = activePresets().flatMap { it.phones }.toSet()
    fun selectedPhones() = (presetPhones() + manualPhones).filterNot { it in excludedPhones }.toSet()
    fun isPhoneSelected(phone: String?) = phone != null && phone in selectedPhones()
    fun togglePhone(phone: String?) {
        if (phone.isNullOrBlank()) return
        if (isPhoneSelected(phone)) {
            manualPhones.remove(phone)
            if (phone in presetPhones() && phone !in excludedPhones) excludedPhones.add(phone)
        } else {
            excludedPhones.remove(phone)
            if (phone !in manualPhones) manualPhones.add(phone)
        }
    }

    private fun keywords() = activePresets().flatMap { it.labelKeywords }.toSet()
    private fun shortSenders() = activePresets().flatMap { it.shortSenders }.toSet()
    fun selectedSms(): List<CorpusSms> {
        val seed = corpus.value ?: return emptyList(); val phones = selectedPhones(); val keys = keywords(); val short = shortSenders()
        return (seed.sms + seed.mms).filter { s -> s.phone in phones || s.rawSender in short || keys.any { k -> s.contact?.contains(k, true) == true || s.rawSender?.contains(k, true) == true } }.sortedByDescending { it.at ?: "" }
    }
    fun selectedCalls(): List<CorpusCall> {
        val seed = corpus.value ?: return emptyList(); val phones = selectedPhones(); val keys = keywords()
        return seed.calls.filter { c -> c.phone in phones || keys.any { k -> c.contact?.contains(k, true) == true } }.sortedByDescending { it.start ?: "" }
    }
    fun selectedRecordings(): List<CorpusRecording> {
        val seed = corpus.value ?: return emptyList(); val phones = selectedPhones(); val keys = keywords()
        return seed.recordings.filter { r -> r.phone in phones || keys.any { k -> r.contact?.contains(k, true) == true } }.sortedByDescending { it.resolvedTime ?: it.callStart ?: "" }
    }
    fun selectedEmails(): List<EmailIdentity> {
        val seed = corpus.value ?: return emptyList(); val ps = activePresets()
        val explicit = ps.flatMap { it.emails }.map { it.lowercase() }.toSet(); val groups = ps.flatMap { it.emailGroups }.toSet(); val domains = ps.flatMap { it.emailDomains }.map { it.lowercase() }.toSet()
        return seed.emailIdentities.filter { e -> e.email.lowercase() in explicit || e.group in groups || domains.any { d -> e.email.lowercase().endsWith("@$d") } }.distinctBy { it.email.lowercase() }.sortedBy { it.email }
    }
    fun emailDomains() = activePresets().flatMap { it.emailDomains }.toSet()
    fun labelFor(phone: String?) = corpus.value?.contacts?.firstOrNull { it.phone == phone }?.label ?: phone ?: "Nieznany"

    /** Seed catalogue matching remains a name-based candidate relation, not an identity claim. */
    private fun installScan(scan: SourceScanResult) {
        sourceScan.value = scan
        val audio = scan.files.filter { it.kind == "audio" }
        linkedRecordingCandidates.clear()
        linkedRecordingCandidates.putAll(audio.groupBy { it.name }.mapValues { (_, files) -> files.map(::asCandidate) })
        scanFiles.value = scan.files.size
        scanDirectories.value = scan.scannedDirectories
        scanSamples.value = scan.files.take(12).map { it.name }
        indexedTree.value = scan.rootUri
    }

    private fun asCandidate(file: ScannedSourceFile) = RecordingCandidate(
        file.name, Uri.parse(file.uri), file.relativePath, file.sizeBytes ?: -1L
    )

    fun toggleSource(uri: String) {
        if (sourceScan.value?.files?.none { it.uri == uri && it.kind == "audio" } != false) return
        if (uri in selectedSourceUris) selectedSourceUris.remove(uri) else selectedSourceUris.add(uri)
    }

    fun selectSourceAudio() {
        selectedSourceUris.clear()
        selectedSourceUris.addAll(sourceScan.value?.files.orEmpty().filter { it.kind == "audio" }.map { it.uri }.distinct())
    }

    fun clearSourceSelection() { selectedSourceUris.clear() }
    fun selectedSourceCandidates() = sourceScan.value?.files.orEmpty()
        .filter { it.kind == "audio" && it.uri in selectedSourceUris }.map(::asCandidate).distinctBy { it.uri.toString() }
    fun sourceCandidate(file: ScannedSourceFile) = asCandidate(file)

    fun cancelSourceScan() {
        // Invalidation shares the final-move lock; cancellation alone cannot fence a late writer.
        scanPublication.invalidate()
        scanJob?.cancel()
        scanning = false
        sourceScanning.value = false
        progress.value = null
        message.value = "Skan anulowany. Ostatni zapisany cache pozostaje historycznym wynikiem."
        updateBusy()
    }

    fun indexRecordings(treeUri: Uri) = scanSources(treeUri)

    fun scanSources(treeUri: Uri, persistentAccess: Boolean = true) {
        if (importing) return
        val publicationToken = scanPublication.begin()
        scanJob?.cancel()
        selectedSourceUris.clear()
        sourceScan.value = null
        sourceScanCached.value = false
        sourceCacheNotice.value = null
        sourceAccessNotice.value = if (persistentAccess) null else
            "Dostawca nie udzielił trwałego dostępu. Po ponownym uruchomieniu może być konieczny ponowny wybór folderu."
        linkedRecordingCandidates.clear()
        scanFiles.value = 0
        scanDirectories.value = 0
        scanSamples.value = emptyList()
        indexedTree.value = null
        message.value = null
        scanning = true
        sourceScanning.value = true
        updateBusy()
        scanJob = viewModelScope.launch {
            progress.value = "Odczytuję folder i surowe metadane…"
            error.value = null
            try {
                val scan = withContext(Dispatchers.IO) { SourceScanner(getApplication()).scan(treeUri) }
                if (!scanPublication.isCurrent(publicationToken)) return@launch
                installScan(scan)
                message.value = "Skan: ${scan.files.size} plików · ${scan.scannedDirectories} folderów. Wybierz nagrania i przejrzyj ograniczenia odczytu."
                // Snapshot output is visible even if the bounded private cache cannot be saved.
                val saved = withContext(Dispatchers.IO) {
                    val scanContext = currentCoroutineContext()
                    runCatching {
                        sourceCache.writeGuarded(scan, scanPublication, publicationToken) { scanContext.ensureActive() }
                    }.also { result ->
                        result.exceptionOrNull()?.let { if (it is CancellationException) throw it }
                    }
                }
                if (!scanPublication.isCurrent(publicationToken) || saved.getOrNull() == false) return@launch
                saved.onFailure {
                    sourceCacheNotice.value = "Bieżący wynik jest tylko w pamięci: ${it.message}. Poprzedni cache, jeśli istniał, pozostał niezmieniony."
                }
                store.saveRecordingTree(treeUri)
            } catch (t: Throwable) {
                if (t is CancellationException) throw t
                if (scanPublication.isCurrent(publicationToken)) error.value = "Skan źródeł: ${t.message}"
            } finally {
                if (scanPublication.isCurrent(publicationToken)) {
                    scanning = false
                    sourceScanning.value = false
                    progress.value = null
                    updateBusy()
                }
            }
        }
    }

    override fun onCleared() {
        // A move that already linearized may remain; a move after invalidation is refused.
        scanPublication.invalidate()
        scanJob?.cancel()
        super.onCleared()
    }

    fun candidates(rec: CorpusRecording) = linkedRecordingCandidates[rec.name].orEmpty()
    fun linkedSelectedCount() = selectedRecordings().count { candidates(it).isNotEmpty() }
    fun selectedCandidates() = selectedRecordings().flatMap(::candidates).distinctBy { it.uri.toString() }

    fun prepareTranscription(activity: ComponentActivity, candidates: List<RecordingCandidate>) {
        try {
            activity.startActivity(PendingRecordingStore(activity).prepare(candidates))
            error.value = null
        } catch (t: Throwable) { error.value = "Przekazanie wyboru: ${t.message}" }
    }

    fun openCandidate(activity: ComponentActivity, candidate: RecordingCandidate, mime: String?) {
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(candidate.uri, mime ?: "audio/*")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            })
            error.value = null
        } catch (t: Throwable) { error.value = "Nie mogę otworzyć ${candidate.relativePath}: ${t.message}" }
    }

    fun openRecording(activity: ComponentActivity, rec: CorpusRecording) {
        try {
            val candidates = candidates(rec)
            require(candidates.size <= 1) { "Nazwa ma ${candidates.size} kandydatów; wybierz konkretny plik" }
            val local = candidates.singleOrNull()?.uri
            val intent = if (local != null) {
                Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(local, rec.mime ?: "audio/*")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
            } else {
                val url = rec.driveUrl ?: error("Brak URI i URL Drive dla ${rec.name}")
                Intent(Intent.ACTION_VIEW, Uri.parse(url))
            }
            activity.startActivity(intent)
            error.value = null
        } catch (t: Throwable) {
            error.value = "Nie mogę otworzyć ${rec.name}: ${t.message}"
        }
    }
}
