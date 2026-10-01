package dev.klbt.ageds

import androidx.compose.runtime.*
import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import kotlinx.coroutines.*

/** All requests capture their endpoint and version before suspension. */
interface CitationService {
    suspend fun transcriptVersionsPage(id: Long, limit: Int = 100, beforeId: Long? = null, snapshotMaxId: Long? = null): ArtifactPage<TranscriptVersion>
    suspend fun transcript(id: Long, versionId: Long): Transcript?
    suspend fun citationsPage(id: Long, limit: Int = 100, beforeId: Long? = null, snapshotMaxId: Long? = null): ArtifactPage<Citation>
    suspend fun annotationsPage(id: Long, limit: Int = 100, beforeId: Long? = null, snapshotMaxId: Long? = null): ArtifactPage<EvidenceAnnotation>
    suspend fun createCitation(id: Long, request: CitationCreate): Citation
    suspend fun annotate(id: Long, request: AnnotationCreate): EvidenceAnnotation
    fun contentUrl(id: Long): String
    fun close()
}

private class HttpCitationService(url: String) : CitationService {
    private val api = EvidenceApi(url)
    override suspend fun transcriptVersionsPage(id: Long, limit: Int, beforeId: Long?, snapshotMaxId: Long?) = api.transcriptVersionsPage(id, limit, beforeId, snapshotMaxId)
    override suspend fun transcript(id: Long, versionId: Long) = api.transcript(id, versionId)
    override suspend fun citationsPage(id: Long, limit: Int, beforeId: Long?, snapshotMaxId: Long?) = api.citationsPage(id, limit, beforeId, snapshotMaxId)
    override suspend fun annotationsPage(id: Long, limit: Int, beforeId: Long?, snapshotMaxId: Long?) = api.annotationsPage(id, limit, beforeId, snapshotMaxId)
    override suspend fun createCitation(id: Long, request: CitationCreate) = api.createCitation(id, request)
    override suspend fun annotate(id: Long, request: AnnotationCreate) = api.annotate(id, request)
    override fun contentUrl(id: Long) = api.contentUrl(id)
    override fun close() = api.close()
}

/** A separate bounded snapshot/cancellation fence for each collection. */
class CitationPages<T>(
    private val scope: CoroutineScope,
    private val idOf: (T) -> Long,
    private val artifactIdOf: (T) -> Long?,
    private val rowSizeOf: (T) -> Long,
) {
    val items = mutableStateListOf<T>()
    val retainedPayloadBytes = mutableStateOf(0L)
    val reachedPayloadLimit = mutableStateOf(false)
    val loading = mutableStateOf(false)
    val error = mutableStateOf<String?>(null)
    val canLoadMore = mutableStateOf(false)
    val coverage = mutableStateOf("Nie odczytano listy.")
    private val epoch = RequestEpoch()
    private var work: Job? = null
    private var chain: ArtifactPageChain<T>? = null

    fun cancel() { epoch.invalidate(); work?.cancel(); work = null; loading.value = false }
    fun reset(id: Long?) {
        cancel(); items.clear(); error.value = null
        retainedPayloadBytes.value = 0L; reachedPayloadLimit.value = false
        chain = id?.let { ArtifactPageChain(artifactId = it, limit = 100, maxItems = 1000, idOf = idOf, artifactIdOf = artifactIdOf, rowSizeOf = rowSizeOf) }
        canLoadMore.value = id != null; coverage.value = "Nie odczytano listy."
    }
    fun load(fetch: suspend (Int, Long?, Long?) -> ArtifactPage<T>, onAccepted: () -> Unit = {}) {
        val previous = chain ?: return
        if (loading.value || !previous.canLoadMore) return
        val token = epoch.invalidate()
        loading.value = true; error.value = null
        work = scope.launch {
            try {
                val page = fetch(100, previous.nextBeforeId, previous.snapshotMaxId)
                val next = previous.append(page)
                if (epoch.accepts(token)) {
                    chain = next; items.clear(); items.addAll(next.items)
                    retainedPayloadBytes.value = next.retainedPayloadBytes
                    reachedPayloadLimit.value = next.reachedPayloadLimit
                    canLoadMore.value = next.canLoadMore
                    coverage.value = when {
                        next.reachedPayloadLimit -> "Pokazano ${items.size} wpisów (${next.retainedPayloadBytes} bajtów JSON UTF-8). Osiągnięto budżet treści 4 MiB; dalsze wpisy pominięto, wczytywanie zatrzymane. To limit zakodowanych danych, nie pamięci aplikacji. Odśwież, aby zacząć nowy snapshot."
                        next.reachedClientLimit -> "Pokazano ${items.size} wpisów. Osiągnięto limit klienta 1000; starsze wpisy nie są widoczne. Odświeżenie rozpoczyna nowy snapshot."
                        next.hasMore -> "Pokazano ${items.size} wpisów; istnieją starsze. Snapshot do ID ${next.snapshotMaxId}."
                        else -> "Pokazano ${items.size} wpisów — koniec tego snapshotu (do ID ${next.snapshotMaxId}). Nowe wpisy wymagają odświeżenia."
                    }
                    onAccepted()
                }
            } catch (t: Exception) {
                if (t is CancellationException) throw t
                if (epoch.accepts(token)) error.value = "Odczyt strony: ${t.message}. Lista pozostaje częściowa. Spróbuj ponownie lub odśwież widok."
            } finally { if (epoch.accepts(token)) loading.value = false }
        }
    }
}

class CitationWorkspace(
    private val scope: CoroutineScope,
    private val serviceFactory: (String) -> CitationService = { HttpCitationService(it) },
    private val audio: RangePlaybackController = RangePlaybackController { AndroidRangeAudio() },
) {
    val transcript = mutableStateOf<Transcript?>(null)
    val versionPages = CitationPages<TranscriptVersion>(scope, { it.id }, { it.artifactId }, { serializedHistoryRowBytes(TranscriptVersion.serializer(), it) })
    val citationPages = CitationPages<Citation>(scope, { it.id }, { it.artifactId }, { serializedHistoryRowBytes(Citation.serializer(), it) })
    val annotationPages = CitationPages<EvidenceAnnotation>(scope, { it.id }, { it.artifactId }, { serializedHistoryRowBytes(EvidenceAnnotation.serializer(), it) })
    val versions get() = versionPages.items
    val citations get() = citationPages.items
    val annotations get() = annotationPages.items
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
    private fun cancelPages() { versionPages.cancel(); citationPages.cancel(); annotationPages.cancel() }
    private fun resetPages(id: Long?) { versionPages.reset(id); citationPages.reset(id); annotationPages.reset(id) }
    fun deactivate() {
        epoch.invalidate(); work?.cancel(); work = null; busy.value = false; stopAudio(); cancelPages()
    }
    fun clear() {
        epoch.invalidate(); work?.cancel(); work = null
        stopAudio(); api?.close(); api = null; artifactId = null
        transcript.value = null; resetPages(null)
        preview.value = null; busy.value = false; error.value = null; message.value = null
    }

    fun open(serverUrl: String, id: Long) {
        clear()
        artifactId = id
        api = serviceFactory(serverUrl)
        refresh()
    }

    /** Explicit refresh resets all three collection snapshots, retaining the selected version. */
    fun refresh() {
        val server = api ?: return
        val id = artifactId ?: return
        val pinnedId = transcript.value?.id
        deactivate(); resetPages(id)
        transcript.value = null; preview.value = null; error.value = null; message.value = null
        versionPages.load({ limit, before, snapshot -> server.transcriptVersionsPage(id, limit, before, snapshot) }) {
            readTranscript(pinnedId ?: versions.firstOrNull()?.id, cancelPaging = false)
        }
        loadOlderCitations(); loadOlderAnnotations()
    }

    fun loadOlderVersions() {
        val server = api ?: return
        val id = artifactId ?: return
        versionPages.load({ limit, before, snapshot -> server.transcriptVersionsPage(id, limit, before, snapshot) }) {
            if (transcript.value == null && !busy.value) readTranscript(versions.firstOrNull()?.id, cancelPaging = false)
        }
    }
    fun loadOlderCitations() {
        val server = api ?: return
        val id = artifactId ?: return
        citationPages.load({ limit, before, snapshot -> server.citationsPage(id, limit, before, snapshot) })
    }
    fun loadOlderAnnotations() {
        val server = api ?: return
        val id = artifactId ?: return
        annotationPages.load({ limit, before, snapshot -> server.annotationsPage(id, limit, before, snapshot) })
    }

    fun load(versionId: Long?) = readTranscript(versionId ?: versions.firstOrNull()?.id, cancelPaging = true)

    private fun readTranscript(versionId: Long?, cancelPaging: Boolean) {
        val server = api ?: return
        val id = artifactId ?: return
        val token = epoch.invalidate()
        work?.cancel(); stopAudio()
        if (cancelPaging) cancelPages()
        preview.value = null; transcript.value = null; error.value = null; message.value = null
        if (versionId == null) { busy.value = false; return }
        busy.value = true
        work = scope.launch {
            try {
                val text = server.transcript(id, versionId)
                require(text != null && text.id == versionId && text.artifactId == id) { "Odpowiedź nie wskazuje wybranej wersji" }
                if (epoch.accepts(token)) transcript.value = text
            } catch (t: Exception) {
                if (t is CancellationException) throw t
                if (epoch.accepts(token)) error.value = "Odczyt wersji: ${t.message}"
            } finally { if (epoch.accepts(token)) busy.value = false }
        }
    }

    fun select(indices: List<Int>, words: List<WordRef>? = null) {
        // A changed selection invalidates an outstanding save's UI response as well.
        epoch.invalidate(); work?.cancel(); busy.value = false; stopAudio(); cancelPages()
        error.value = null; message.value = null
        preview.value = try {
            val text = transcript.value ?: return
            if (words == null) CitationSelection.segments(text, indices) else CitationSelection.words(text, words)
        } catch (t: IllegalArgumentException) { error.value = t.message; null }
    }

    fun clearSelection() {
        epoch.invalidate(); work?.cancel(); busy.value = false
        preview.value = null; stopAudio(); error.value = null; cancelPages()
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
                    citationPages.reset(selected.artifactId); loadOlderCitations()
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
            if (epoch.accepts(token)) {
                annotationPages.reset(id); loadOlderAnnotations()
                message.value = "Zapisano adnotację #${saved.id}; odświeżono snapshot adnotacji."
                true
            } else false
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
