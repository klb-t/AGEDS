package dev.klbt.ageds

import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import dev.klbt.ageds.core.AnnotationCreate
import dev.klbt.ageds.core.ArtifactPage
import dev.klbt.ageds.core.Citation
import dev.klbt.ageds.core.CitationCreate
import dev.klbt.ageds.core.Transcript
import dev.klbt.ageds.core.TranscriptSegment
import dev.klbt.ageds.core.TranscriptVersion
import dev.klbt.ageds.core.TranscriptWord
import dev.klbt.ageds.core.WordRef
import dev.klbt.ageds.core.WordTiming
import dev.klbt.ageds.core.WordTimingSegment
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/** Owner-level checks that create responses are fenced before observable save effects. */
class CitationWorkspaceCreatedResponseTest {
    private class Service : CitationService {
        var response: Citation? = null
        var delayed = false
        var continuation: Continuation<Citation>? = null
        var citationPageCalls = 0

        override suspend fun transcriptVersionsPage(
            id: Long,
            limit: Int,
            beforeId: Long?,
            snapshotMaxId: Long?,
        ) = ArtifactPage(id, listOf(TranscriptVersion(41, id)), null, 41, false, limit)

        override suspend fun transcript(id: Long, versionId: Long) = Transcript(
            id = versionId,
            artifactId = id,
            text = "samesame",
            segments = listOf(
                TranscriptSegment(0.1, 0.2, "same", listOf(TranscriptWord(0.1, 0.2, "same"))),
                TranscriptSegment(0.1, 0.2, "same", listOf(TranscriptWord(0.1, 0.2, "same"))),
            ),
            wordTiming = WordTiming(
                status = "available",
                segments = listOf(WordTimingSegment(0, true), WordTimingSegment(1, true)),
            ),
        )

        override suspend fun citationsPage(
            id: Long,
            limit: Int,
            beforeId: Long?,
            snapshotMaxId: Long?,
        ): ArtifactPage<Citation> {
            citationPageCalls++
            return ArtifactPage(id, emptyList(), null, 0, false, limit)
        }

        override suspend fun annotationsPage(
            id: Long,
            limit: Int,
            beforeId: Long?,
            snapshotMaxId: Long?,
        ) = ArtifactPage<EvidenceAnnotation>(id, emptyList(), null, 0, false, limit)

        override suspend fun createCitation(id: Long, request: CitationCreate): Citation {
            if (delayed) return suspendCoroutine { continuation = it }
            return requireNotNull(response)
        }

        override suspend fun annotate(id: Long, request: AnnotationCreate): EvidenceAnnotation =
            error("No annotation write expected")

        override fun contentUrl(id: Long) = "fixture/$id"
        override fun close() = Unit
    }

    private class Fixture {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        val service = Service()
        val workspace = CitationWorkspace(
            scope,
            { service },
            RangePlaybackController { error("No decoder expected") },
        )

        fun selectWord(segmentIndex: Int) {
            workspace.open("fixture", 7)
            workspace.select(emptyList(), listOf(WordRef(segmentIndex, 0)))
        }

        fun close() {
            workspace.clear()
            scope.cancel()
        }
    }

    @Test
    fun exactCreatedWordProjectionIsAcceptedBeforeHistoryRefreshAndSuccess() {
        val fixture = Fixture()
        try {
            fixture.selectWord(0)
            val callsBeforeSave = fixture.service.citationPageCalls
            fixture.service.response = citation(wordSelector(0))

            fixture.workspace.save()

            assertEquals(callsBeforeSave + 1, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.error.value)
            assertEquals("Zapisano cytat #81, wersja #41.", fixture.workspace.message.value)
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    @Test
    fun sameTextAndRoundedRangeWithDifferentWordRefIsRejectedWithoutSaveEffects() {
        val fixture = Fixture()
        try {
            fixture.selectWord(0)
            val callsBeforeSave = fixture.service.citationPageCalls
            fixture.service.response = citation(wordSelector(1))

            fixture.workspace.save()

            assertEquals(callsBeforeSave, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.message.value)
            assertNotNull(fixture.workspace.error.value)
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    @Test
    fun canonicalMetadataAndRoundedRangeMismatchesAreRejectedBeforeRefresh() {
        val selector = wordSelector(0)
        val mismatches = listOf(
            citation(JsonObject(selector + ("alignment_verification" to JsonPrimitive("performed")))),
            citation(selector, endMs = 201),
        )
        mismatches.forEach { mismatch ->
            val fixture = Fixture()
            try {
                fixture.selectWord(0)
                val callsBeforeSave = fixture.service.citationPageCalls
                fixture.service.response = mismatch

                fixture.workspace.save()

                assertEquals(callsBeforeSave, fixture.service.citationPageCalls)
                assertNull(fixture.workspace.message.value)
                assertNotNull(fixture.workspace.error.value)
            } finally {
                fixture.close()
            }
        }
    }

    @Test
    fun validLateResponseForPreviousSelectionCannotOverwriteCurrentView() {
        val fixture = Fixture()
        try {
            fixture.selectWord(0)
            val callsBeforeSave = fixture.service.citationPageCalls
            fixture.service.delayed = true
            fixture.workspace.save()
            val continuation = requireNotNull(fixture.service.continuation)

            fixture.workspace.select(emptyList(), listOf(WordRef(1, 0)))
            continuation.resume(citation(wordSelector(0)))

            assertEquals(listOf(WordRef(1, 0)), fixture.workspace.preview.value!!.request.wordRefs)
            assertEquals(callsBeforeSave, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.message.value)
            assertNull(fixture.workspace.error.value)
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    private companion object {
        fun wordSelector(segmentIndex: Int) = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("words"),
                "word_refs" to JsonArray(listOf(JsonObject(mapOf(
                    "segment_index" to JsonPrimitive(segmentIndex),
                    "word_index" to JsonPrimitive(0),
                )))),
                "text_join" to JsonPrimitive("concatenate_exact"),
                "time_unit" to JsonPrimitive("seconds"),
                "stored_time_unit" to JsonPrimitive("milliseconds"),
                "rounding" to JsonPrimitive("nearest_ms"),
                "precision" to JsonPrimitive("word_asr"),
                "source_start" to JsonPrimitive(0.1),
                "source_end" to JsonPrimitive(0.2),
                "alignment_verification" to JsonPrimitive("not_performed"),
            ),
        )

        fun citation(selector: JsonObject, endMs: Long = 200) = Citation(
            id = 81,
            artifactId = 7,
            derivedTextId = 41,
            startMs = 100,
            endMs = endMs,
            quoteText = "same",
            quoteSha256 = "fixture",
            selector = selector,
        )
    }
}
