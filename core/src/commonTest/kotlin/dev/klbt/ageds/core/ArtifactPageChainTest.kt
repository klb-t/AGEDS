package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class ArtifactPageChainTest {
    private fun chain(limit: Int = 2, maxItems: Int = 1000) = ArtifactPageChain<TranscriptVersion>(
        artifactId = 9, limit = limit, maxItems = maxItems, idOf = { it.id }, artifactIdOf = { it.artifactId })
    private fun page(ids: List<Long>, more: Boolean, snapshot: Long = 10, limit: Int = 2) =
        ArtifactPage(9, ids.map { TranscriptVersion(it, 9) }, if (more) ids.lastOrNull() else null, snapshot, more, limit)

    @Test fun completeDescendingSnapshotChain() {
        val initial = chain()
        assertTrue(initial.canLoadMore)
        val first = initial.append(page(listOf(10, 8), true))
        val final = first.append(page(listOf(4, 1), false))
        assertEquals(listOf(10L, 8L, 4L, 1L), final.items.map { it.id })
        assertEquals(10L, final.snapshotMaxId)
        assertFalse(final.canLoadMore)
        assertFalse(final.reachedClientLimit)
        assertFalse(initial.isLoaded)
        assertEquals(listOf(10L, 8L), first.items.map { it.id })
    }

    @Test fun shortPageCanContinueForServerByteBudget() {
        val first = chain().append(page(listOf(10), true))
        assertEquals(10L, first.nextBeforeId)
        assertTrue(first.canLoadMore)
        assertEquals(listOf(10L, 4L), first.append(page(listOf(4), false)).items.map { it.id })
    }

    @Test fun emptyInitialSnapshotIsTerminal() {
        val empty = chain().append(page(emptyList(), false, snapshot = 0))
        assertTrue(empty.isLoaded)
        assertTrue(empty.items.isEmpty())
        assertFalse(empty.canLoadMore)
        assertFailsWith<IllegalArgumentException> { empty.append(page(listOf(2), false)) }
    }

    @Test fun finalPartialPageIsExplicitlyTruncatedAtClientCap() {
        val first = chain(maxItems = 3).append(page(listOf(10, 8), true))
        val limited = first.append(page(listOf(4, 2), false))
        assertEquals(listOf(10L, 8L, 4L), limited.items.map { it.id })
        assertTrue(limited.reachedClientLimit)
        assertTrue(limited.hasMore)
        assertFalse(limited.canLoadMore)
        assertEquals(4L, limited.nextBeforeId)
        assertFailsWith<IllegalArgumentException> { limited.append(page(listOf(2), false)) }
    }

    @Test fun invalidLaterPageCannotChangeAcceptedChain() {
        val first = chain().append(page(listOf(10, 8), true))
        assertFailsWith<IllegalArgumentException> { first.append(page(listOf(8, 4), false)) }
        assertEquals(listOf(10L, 8L), first.items.map { it.id })
        assertEquals(8L, first.nextBeforeId)
        assertTrue(first.canLoadMore)
    }

    @Test fun typedEnvelopePreservesExistingItemWireShapes() {
        val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
        val version = json.decodeFromString<ArtifactPage<TranscriptVersion>>("""{"artifactId":9,"items":[{"id":10,"artifact_id":9,"run_id":3}],"nextBeforeId":null,"snapshotMaxId":10,"hasMore":false,"limit":2}""")
        assertEquals(3L, chain().append(version).items.single().runId)
        val annotation = json.decodeFromString<ArtifactPage<Annotation>>("""{"artifactId":9,"items":[{"id":10,"artifactId":9,"body":"raw"}],"nextBeforeId":null,"snapshotMaxId":10,"hasMore":false,"limit":2}""")
        assertEquals("raw", annotation.items.single().body)
        assertEquals(9L, annotation.items.single().artifactId)
    }

    @Test fun fullDefaultMaximumStopsAtExactlyOneThousand() {
        var chain = chain(limit = 100)
        for (offset in 0 until 1000 step 100) {
            val ids = (2000L - offset downTo 1901L - offset).toList()
            chain = chain.append(page(ids, true, snapshot = 2000, limit = 100))
        }
        assertEquals(1000, chain.items.size)
        assertTrue(chain.reachedClientLimit)
        assertFalse(chain.canLoadMore)
    }
}
