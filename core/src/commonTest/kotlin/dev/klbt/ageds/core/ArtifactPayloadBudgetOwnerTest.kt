package dev.klbt.ageds.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class ArtifactPayloadBudgetOwnerTest {
    private fun row(id: Long, model: String = "raw") = TranscriptVersion(id, 7, model = model)
    private fun chain(budget: Long) = ArtifactPageChain<TranscriptVersion>(
        artifactId = 7, limit = 2, idOf = { it.id }, artifactIdOf = { it.artifactId },
        maxPayloadBytes = budget, rowSizeOf = { serializedHistoryRowBytes(TranscriptVersion.serializer(), it) })

    @Test fun actualSerializedPrefixStopsBeforeFirstUnfitRow() {
        val first = row(9, "Zażółć 😀 \"literal\"\n")
        val second = row(8, "larger".repeat(40))
        val budget = serializedHistoryRowBytes(TranscriptVersion.serializer(), first) + 1
        val state = chain(budget).append(ArtifactPage(7, listOf(first, second), null, 9, false, 2))
        assertEquals(listOf(first), state.items)
        assertEquals(budget - 1, state.retainedPayloadBytes)
        assertEquals(9L, state.nextBeforeId)
        assertTrue(state.reachedPayloadLimit)
        assertTrue(state.hasMore)
        assertFalse(state.canLoadMore)
        assertFalse(state.reachedClientLimit)
    }

    @Test fun positiveCostRequiredForEveryMeasuredRowBeforeAnyAdmission() {
        val state = ArtifactPageChain<TranscriptVersion>(7, limit = 2, idOf = { it.id },
            artifactIdOf = { it.artifactId }, maxPayloadBytes = 1,
            rowSizeOf = { if (it.id == 8L) 0L else 100L })
        assertFailsWith<IllegalArgumentException> {
            state.append(ArtifactPage(7, listOf(row(9), row(8)), null, 9, false, 2))
        }
        assertTrue(state.items.isEmpty())
        assertEquals(0L, state.retainedPayloadBytes)
    }

    @Test fun omittedMeasurementIsExplicitlyCountOnlyNotPretendStringBytes() {
        val state = ArtifactPageChain<TranscriptVersion>(7, limit = 2, idOf = { it.id }, artifactIdOf = { it.artifactId })
        assertFalse(state.payloadBudgetEnabled)
        assertTrue(chain(100).payloadBudgetEnabled)
    }
}
