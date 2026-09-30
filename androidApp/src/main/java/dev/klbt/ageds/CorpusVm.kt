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

private val defaultPresetIds = listOf("gemeente_oss", "uwv", "susanne_walstra", "wettbewind", "acture")

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
    private val manualPhones = mutableStateListOf<String>()
    private val excludedPhones = mutableStateListOf<String>()
    private val store = CorpusStore(application)
    private var scanJob: Job? = null
    private var scanGeneration = 0
    private var importing = false
    private var scanning = false
    private fun updateBusy() { busy.value = importing || scanning }

    init { store.loadCached()?.let { install(it) } }

    private fun install(seed: CorpusSeed) {
        corpus.value = seed
        linkedRecordingCandidates.clear()
        scanFiles.value = 0
        scanDirectories.value = 0
        scanSamples.value = emptyList()
        indexedTree.value = null
        selectPriority()
        store.savedRecordingTree()?.let { indexRecordings(it) }
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
        selectedPresetIds.clear(); selectedPresetIds.addAll(defaultPresetIds)
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

    fun indexRecordings(treeUri: Uri) {
        val seed = corpus.value ?: return
        val generation = ++scanGeneration
        scanJob?.cancel()
        scanning = true
        updateBusy()
        scanJob = viewModelScope.launch {
            progress.value = "Indeksuję nagrania…"
            error.value = null
            try {
                val scan = withContext(Dispatchers.IO) { store.indexRecordingTree(treeUri, seed.recordings.map { it.name }.toSet()) }
                if (generation != scanGeneration) return@launch
                linkedRecordingCandidates.clear()
                linkedRecordingCandidates.putAll(scan.found)
                scanFiles.value = scan.scannedFiles
                scanDirectories.value = scan.scannedDirectories
                scanSamples.value = scan.sampleNames
                indexedTree.value = treeUri.toString()
                val selected = linkedSelectedCount()
                message.value = "Przeskanowano ${scan.scannedFiles} plików w ${scan.scannedDirectories} folderach · znaleziono ${scan.found.values.sumOf { it.size }} kandydatów dla ${scan.found.size} nazw · z aktualnego wyboru $selected/${selectedRecordings().size} pozycji katalogu."
                if (scan.scannedFiles == 200 && seed.recordings.size > 200) {
                    message.value += " Provider zwrócił 200 plików; kompletność skanu nie została potwierdzona. Możesz wskazać mniejszy folder lub pliki bezpośrednio."
                }
            } catch (t: Throwable) {
                if (t is CancellationException) throw t
                if (generation == scanGeneration) error.value = "Nagrania: ${t.message}"
            } finally {
                if (generation == scanGeneration) {
                    scanning = false
                    progress.value = null
                    updateBusy()
                }
            }
        }
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
