package dev.klbt.ageds

import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import dev.klbt.ageds.core.AnnotationCreate
import dev.klbt.ageds.core.ArtifactPage
import dev.klbt.ageds.core.Citation
import dev.klbt.ageds.core.CitationCreate
import dev.klbt.ageds.core.Transcript
import dev.klbt.ageds.core.TranscriptSegment
import dev.klbt.ageds.core.TranscriptVersion
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
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/** Independent acceptance checks for the response fence in [CitationWorkspace.save]. */
class CitationWorkspaceResponseAdversarialTest {
    private class Service : CitationService {
        var response: Citation? = null
        var delaySave = false
        var heldSave: Continuation<Citation>? = null
        var citationPageCalls = 0
        var published: Citation? = null

        private val existing = citation(
            id = 70,
            selector = segmentSelector(1),
            quoteText = "same",
        )

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
            // The deliberately repeated text and identical time interval make the
            // selector the only field that identifies the chosen occurrence.
            segments = listOf(
                TranscriptSegment(0.1, 0.2, "same"),
                TranscriptSegment(0.1, 0.2, "same"),
            ),
        )

        override suspend fun citationsPage(
            id: Long,
            limit: Int,
            beforeId: Long?,
            snapshotMaxId: Long?,
        ): ArtifactPage<Citation> {
            citationPageCalls++
            val items = listOf(published ?: existing)
            return ArtifactPage(id, items, null, items.single().id, false, limit)
        }

        override suspend fun annotationsPage(
            id: Long,
            limit: Int,
            beforeId: Long?,
            snapshotMaxId: Long?,
        ) = ArtifactPage<EvidenceAnnotation>(id, emptyList(), null, 0, false, limit)

        override suspend fun createCitation(id: Long, request: CitationCreate): Citation {
            if (delaySave) return suspendCoroutine { heldSave = it }
            return requireNotNull(response).also { published = it }
        }

        override suspend fun annotate(id: Long, request: AnnotationCreate): EvidenceAnnotation =
            error("No annotation writes expected")

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

        fun openAndSelect(index: Int = 0) {
            workspace.open("fixture", 7)
            workspace.select(listOf(index))
        }

        fun close() {
            workspace.clear()
            scope.cancel()
        }
    }

    @Test
    fun sameTextAndTimeFromDifferentOccurrenceIsRejectedBeforeHistoryRefreshOrSuccess() {
        val fixture = Fixture()
        try {
            fixture.openAndSelect(0)
            val initialPageCalls = fixture.service.citationPageCalls
            fixture.service.response = citation(id = 81, selector = segmentSelector(1))

            fixture.workspace.save()

            assertEquals(listOf(70L), fixture.workspace.citations.map { it.id })
            assertEquals(initialPageCalls, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.message.value)
            assertNotNull(fixture.workspace.error.value)
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    @Test
    fun nonPositiveCreatedIdIsRejectedWithoutDiscardingCurrentPage() {
        val fixture = Fixture()
        try {
            fixture.openAndSelect()
            val initialPageCalls = fixture.service.citationPageCalls
            fixture.service.response = citation(id = 0, selector = segmentSelector(0))

            fixture.workspace.save()

            assertEquals(listOf(70L), fixture.workspace.citations.map { it.id })
            assertEquals(initialPageCalls, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.message.value)
            assertTrue(fixture.workspace.error.value.orEmpty().startsWith("Zapis cytatu:"))
        } finally {
            fixture.close()
        }
    }

    @Test
    fun exactSelectedSelectorIsAcceptedAndThenRefreshesCitationHistory() {
        val fixture = Fixture()
        try {
            fixture.openAndSelect()
            val initialPageCalls = fixture.service.citationPageCalls
            fixture.service.response = citation(id = 81, selector = segmentSelector(0))

            fixture.workspace.save()

            assertEquals(listOf(81L), fixture.workspace.citations.map { it.id })
            assertEquals(initialPageCalls + 1, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.error.value)
            assertTrue(fixture.workspace.message.value.orEmpty().contains("#81"))
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    @Test
    fun staleValidResponseAfterSelectionChangeCannotMutateHistoryOrStatus() {
        val fixture = Fixture()
        try {
            fixture.openAndSelect(0)
            val initialPageCalls = fixture.service.citationPageCalls
            fixture.service.delaySave = true
            fixture.workspace.save()
            val held = requireNotNull(fixture.service.heldSave)

            fixture.workspace.select(listOf(1))
            held.resume(citation(id = 81, selector = segmentSelector(0)))

            assertEquals(1, fixture.workspace.preview.value!!.request.segmentIndices!!.single())
            assertEquals(listOf(70L), fixture.workspace.citations.map { it.id })
            assertEquals(initialPageCalls, fixture.service.citationPageCalls)
            assertNull(fixture.workspace.message.value)
            assertNull(fixture.workspace.error.value)
            assertFalse(fixture.workspace.busy.value)
        } finally {
            fixture.close()
        }
    }

    private companion object {
        fun segmentSelector(index: Int) = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("segments"),
                "indices" to JsonArray(listOf(JsonPrimitive(index))),
                "text_join" to JsonPrimitive("concatenate_exact"),
                "time_unit" to JsonPrimitive("seconds"),
                "stored_time_unit" to JsonPrimitive("milliseconds"),
                "rounding" to JsonPrimitive("nearest_ms"),
                "precision" to JsonPrimitive("segment"),
            ),
        )

        fun citation(
            id: Long,
            selector: JsonObject,
            quoteText: String = "same",
        ) = Citation(
            id = id,
            artifactId = 7,
            derivedTextId = 41,
            startMs = 100,
            endMs = 200,
            quoteText = quoteText,
            quoteSha256 = "fixture",
            selector = selector,
        )
    }
}
