package dev.klbt.ageds

import android.app.Application
import android.net.Uri
import androidx.activity.ComponentActivity
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.AndroidViewModel
import dev.klbt.ageds.core.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private val defaultPresetIds = listOf("gemeente_oss", "uwv", "susanne_walstra", "wettbewind", "acture")

class CorpusVm(application: Application) : AndroidViewModel(application) {
    val corpus = mutableStateOf<CorpusSeed?>(null)
    val selectedPresetIds = mutableStateListOf<String>()
    val serverUrl = mutableStateOf("http://10.0.2.2:8080")
    val busy = mutableStateOf(false)
    val error = mutableStateOf<String?>(null)
    val message = mutableStateOf<String?>(null)
    val progress = mutableStateOf<String?>(null)
    val linkedRecordingUris = mutableStateMapOf<String, Uri>()
    private val manualPhones = mutableStateListOf<String>()
    private val excludedPhones = mutableStateListOf<String>()
    private val store = CorpusStore(application)
    private val api get() = EvidenceApi(serverUrl.value)

    init { store.loadCached()?.let { install(it) } }

    private fun install(seed: CorpusSeed) {
        corpus.value = seed
        linkedRecordingUris.clear()
        selectPriority()
    }

    suspend fun importCorpus(uri: Uri) {
        busy.value = true
        try {
            val seed = withContext(Dispatchers.IO) { store.import(uri) }
            install(seed)
            message.value = "Wczytano ${seed.contacts.size} numerów · ${seed.sms.size} SMS · ${seed.calls.size} połączeń · ${seed.recordings.size} nagrań."
            error.value = null
        } catch (t: Throwable) { error.value = "Import: ${t.message}" }
        finally { busy.value = false }
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
        return seed.recordings.filter { r -> r.phone in phones || keys.any { k -> r.contact?.contains(k, true) == true } }.distinctBy { it.name }.sortedByDescending { it.resolvedTime ?: it.callStart ?: "" }
    }
    fun selectedEmails(): List<EmailIdentity> {
        val seed = corpus.value ?: return emptyList(); val ps = activePresets()
        val explicit = ps.flatMap { it.emails }.map { it.lowercase() }.toSet(); val groups = ps.flatMap { it.emailGroups }.toSet(); val domains = ps.flatMap { it.emailDomains }.map { it.lowercase() }.toSet()
        return seed.emailIdentities.filter { e -> e.email.lowercase() in explicit || e.group in groups || domains.any { d -> e.email.lowercase().endsWith("@$d") } }.distinctBy { it.email.lowercase() }.sortedBy { it.email }
    }
    fun emailDomains() = activePresets().flatMap { it.emailDomains }.toSet()
    fun labelFor(phone: String?) = corpus.value?.contacts?.firstOrNull { it.phone == phone }?.label ?: phone ?: "Nieznany"

    suspend fun indexRecordings(treeUri: Uri) {
        val seed = corpus.value ?: return
        busy.value = true; progress.value = "Indeksuję Recordings…"
        try {
            val found = withContext(Dispatchers.IO) { store.indexRecordingTree(treeUri, seed.recordings.map { it.name }.toSet()) }
            linkedRecordingUris.clear(); linkedRecordingUris.putAll(found)
            message.value = "Znaleziono ${found.size}/${seed.recordings.size} plików nagrań."
            error.value = null
        } catch (t: Throwable) { error.value = "Recordings: ${t.message}" }
        finally { busy.value = false; progress.value = null }
    }
    fun linkedSelectedCount() = selectedRecordings().count { it.name in linkedRecordingUris }

    suspend fun transcribeSelected(activity: ComponentActivity) {
        val targets = selectedRecordings().mapNotNull { r -> linkedRecordingUris[r.name]?.let { r to it } }
        if (targets.isEmpty()) { error.value = "Najpierw połącz folder Recordings; brak dopasowanych plików dla zaznaczonego korpusu."; return }
        busy.value = true
        try {
            for ((i, pair) in targets.withIndex()) {
                val (rec, uri) = pair
                progress.value = "Kolejka ${i + 1}/${targets.size} · ${rec.contact ?: rec.name}"
                val uploaded = withContext(Dispatchers.IO) { api.uploadAudio(activity.contentResolver, uri) }
                val priority = TranscriptionPriority.score(PrioritySignals(manualPriority = 100, durationSeconds = rec.callDurationSec?.toLong(), hasKnownCounterparty = !rec.contact.isNullOrBlank(), hasConflict = !rec.note.isNullOrBlank(), taggedLegal = true))
                api.queueTranscription(uploaded.artifactId, priority)
            }
            message.value = "Dodano ${targets.size} nagrań do batchowej transkrypcji."
            error.value = null
        } catch (t: Throwable) { error.value = "Transkrypcja: ${t.message}" }
        finally { busy.value = false; progress.value = null }
    }
}
