package dev.klbt.ageds

import androidx.compose.runtime.*
import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import kotlinx.coroutines.*

/** All requests capture their endpoint and version before suspension. */
interface CitationService {
    suspend fun transcriptVersions(id: Long): List<TranscriptVersion>
    suspend fun transcript(id: Long, versionId: Long): Transcript?
    suspend fun citations(id: Long): List<Citation>
    suspend fun annotations(id: Long): List<EvidenceAnnotation>
    suspend fun createCitation(id: Long, request: CitationCreate): Citation
    suspend fun annotate(id: Long, request: AnnotationCreate): EvidenceAnnotation
    fun contentUrl(id: Long): String
    fun close()
}

private class HttpCitationService(url: String) : CitationService {
    private val api = EvidenceApi(url)
    override suspend fun transcriptVersions(id: Long) = api.transcriptVersions(id)
    override suspend fun transcript(id: Long, versionId: Long) = api.transcript(id, versionId)
    override suspend fun citations(id: Long) = api.citations(id)
    override suspend fun annotations(id: Long) = api.annotations(id)
    override suspend fun createCitation(id: Long, request: CitationCreate) = api.createCitation(id, request)
    override suspend fun annotate(id: Long, request: AnnotationCreate) = api.annotate(id, request)
    override fun contentUrl(id: Long) = api.contentUrl(id)
    override fun close() = api.close()
}

class CitationWorkspace(
    private val scope: CoroutineScope,
    private val serviceFactory: (String) -> CitationService = { HttpCitationService(it) },
    private val audio: RangePlaybackController = RangePlaybackController { AndroidRangeAudio() },
) {
    val transcript = mutableStateOf<Transcript?>(null)
    val versions = mutableStateListOf<TranscriptVersion>()
    val citations = mutableStateListOf<Citation>()
    val annotations = mutableStateListOf<EvidenceAnnotation>()
    val preview = mutableStateOf<SelectedCitation?>(null)
    val busy = mutableStateOf(false)
    val error = mutableStateOf<String?>(null)
    val message = mutableStateOf<String?>(null)
    val audioStatus = mutableStateOf("Zatrzymane")
    private val epoch = RequestEpoch()
    private var work: Job? = null
    private var api: CitationService? = null
    private var artifactId: Long? = null

    fun stopAudio() { audio.stop(); audioStatus.value = audio.status }
    fun tickAudio() { audio.tick(); audioStatus.value = audio.status }
    fun deactivate() {
        epoch.invalidate(); work?.cancel(); work = null; busy.value = false; stopAudio()
    }
    fun clear() {
        epoch.invalidate(); work?.cancel(); work = null
        stopAudio(); api?.close(); api = null; artifactId = null
        transcript.value = null; versions.clear(); citations.clear(); annotations.clear()
        preview.value = null; busy.value = false; error.value = null; message.value = null
    }

    fun open(serverUrl: String, id: Long) {
        clear()
        artifactId = id
        api = serviceFactory(serverUrl)
        load(null)
    }

    fun load(versionId: Long?) {
        val server = api ?: return
        val id = artifactId ?: return
        val token = epoch.invalidate()
        work?.cancel(); stopAudio()
        preview.value = null; transcript.value = null; error.value = null; message.value = null
        busy.value = true
        work = scope.launch {
            try {
                val listed = server.transcriptVersions(id)
                require(listed.all { it.artifactId == id }) { "Lista wersji wskazuje inny artefakt" }
                val pinnedId = versionId ?: listed.firstOrNull()?.id
                val text = if (pinnedId == null) null else server.transcript(id, pinnedId)
                require(text == null || (text.id == pinnedId && text.artifactId == id)) { "Odpowiedź nie wskazuje wybranej wersji" }
                val saved = server.citations(id)
                require(saved.all { it.artifactId == id }) { "Lista cytatów wskazuje inny artefakt" }
                val notes = server.annotations(id)
                require(notes.all { it.artifactId == id }) { "Lista adnotacji wskazuje inny artefakt" }
                if (epoch.accepts(token)) {
                    versions.clear(); versions.addAll(listed)
                    transcript.value = text
                    citations.clear(); citations.addAll(saved)
                    annotations.clear(); annotations.addAll(notes)
                }
            } catch (t: Exception) {
                if (t is CancellationException) throw t
                if (epoch.accepts(token)) error.value = "Odczyt wersji: ${t.message}"
            } finally { if (epoch.accepts(token)) busy.value = false }
        }
    }

    fun select(indices: List<Int>, words: List<WordRef>? = null) {
        // A changed selection invalidates an outstanding save's UI response as well.
        epoch.invalidate(); work?.cancel(); busy.value = false; stopAudio()
        error.value = null; message.value = null
        preview.value = try {
            val text = transcript.value ?: return
            if (words == null) CitationSelection.segments(text, indices) else CitationSelection.words(text, words)
        } catch (t: IllegalArgumentException) { error.value = t.message; null }
    }

    fun clearSelection() {
        epoch.invalidate(); work?.cancel(); busy.value = false
        preview.value = null; stopAudio(); error.value = null
    }

    fun save() {
        val selected = preview.value ?: return
        val server = api ?: return
        if (busy.value) return
        val token = epoch.current()
        busy.value = true; error.value = null
        work = scope.launch {
            try {
                val saved = server.createCitation(selected.artifactId, selected.request)
                require(saved.artifactId == selected.artifactId && saved.derivedTextId == selected.derivedTextId &&
                    saved.quoteText == selected.quoteText && saved.startMs == selected.startMs && saved.endMs == selected.endMs) {
                    "Odpowiedź zapisu nie odpowiada wybranemu cytatowi"
                }
                if (epoch.accepts(token)) {
                    citations.add(0, saved)
                    message.value = "Zapisano cytat #${saved.id}, wersja #${saved.derivedTextId}."
                }
            } catch (t: Exception) {
                if (t is CancellationException) throw t
                if (epoch.accepts(token)) error.value = "Zapis cytatu: ${t.message}. Po błędzie sieci odśwież listę przed ponowieniem."
            } finally { if (epoch.accepts(token)) busy.value = false }
        }
    }

    suspend fun addNote(body: String): Boolean {
        val server = api ?: return false
        val id = artifactId ?: return false
        val selected = preview.value
        val version = transcript.value?.id
        val token = epoch.current()
        if (busy.value) return false
        busy.value = true
        work = currentCoroutineContext().job
        return try {
            val saved = server.annotate(id, AnnotationCreate(body = body, derivedTextId = version,
                startMs = selected?.startMs, endMs = selected?.endMs))
            require(saved.artifactId == id && saved.derivedTextId == version && saved.body == body &&
                saved.startMs == selected?.startMs && saved.endMs == selected?.endMs) {
                "Odpowiedź zapisu nie odpowiada wybranej adnotacji"
            }
            if (epoch.accepts(token)) { annotations.add(0, saved); true } else false
        } catch (t: Exception) {
            if (t is CancellationException) throw t
            if (epoch.accepts(token)) error.value = "Zapis adnotacji: ${t.message}"
            false
        } finally { if (epoch.accepts(token)) busy.value = false }
    }

    fun play(citation: Citation) {
        val server = api ?: return
        if (citation.artifactId != artifactId) return
        try { audio.play(server.contentUrl(citation.artifactId), citation.startMs, citation.endMs) }
        catch (t: Exception) { error.value = "Audio: ${t.message}" }
        audioStatus.value = audio.status
    }
}
