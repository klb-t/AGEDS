package dev.klbt.ageds.core

import kotlinx.serialization.json.JsonObject
import kotlin.test.*

class ArtifactPayloadBudgetAdversarialTest {
    private fun row(id:Long,text:String="")=Citation(id,7,3,1,4,text,"fixture",JsonObject(emptyMap()))
    private fun cost(c:Citation)=serializedHistoryRowBytes(Citation.serializer(),c)
    private fun chain(budget:Long=ARTIFACT_HISTORY_PAYLOAD_BYTES)=ArtifactPageChain<Citation>(
        artifactId=7,limit=100,idOf={it.id},artifactIdOf={it.artifactId},maxPayloadBytes=budget,rowSizeOf=::cost)
    private fun page(items:List<Citation>,more:Boolean=false)=ArtifactPage(7,items,if(more) items.last().id else null,100,false,100).copy(hasMore=more)

    @Test fun productionSerializationCostCountsUtf8EscapingDefaultsAndNulls() {
        val version=TranscriptVersion(10,7,model="ą🐈\"\n\u0000")
        val wire="""{"id":10,"artifact_id":7,"run_id":null,"model":"ą🐈\"\n\u0000","language":null,"created_at":null}"""
        assertEquals(wire.encodeToByteArray().size.toLong(),serializedHistoryRowBytes(TranscriptVersion.serializer(),version))
        assertEquals(16L,cost(row(10,"ą🐈\"\n\u0000"))-cost(row(10)))
    }
    @Test fun exactTerminalByteBoundaryIsComplete() {
        val a=row(10,"ąć");val b=row(9,"🐈");val bytes=cost(a)+cost(b)
        val c=chain(bytes).append(page(listOf(a,b)))
        assertEquals(bytes,c.retainedPayloadBytes);assertEquals(2,c.items.size)
        assertTrue(c.payloadBudgetEnabled);assertFalse(c.reachedPayloadLimit);assertFalse(c.hasMore)
    }
    @Test fun exactContinuingByteBoundaryStopsFurtherFetches() {
        val a=row(10);val c=chain(cost(a)).append(page(listOf(a),true))
        assertTrue(c.reachedPayloadLimit);assertTrue(c.hasMore);assertFalse(c.canLoadMore)
    }
    @Test fun oversizedFirstRowDoesNotSkipToSmallerLaterRow() {
        val a=row(10,"x".repeat(100));val c=chain(cost(a)-1).append(page(listOf(a,row(9))))
        assertTrue(c.items.isEmpty());assertEquals(0L,c.retainedPayloadBytes);assertTrue(c.reachedPayloadLimit)
        assertTrue(c.hasMore);assertFalse(c.canLoadMore)
    }
    @Test fun prefixAdmissionKeepsLastAdmittedCursorInsteadOfLastReturnedCursor() {
        val a=row(10);val b=row(9,"x".repeat(100));val small=row(8)
        val c=chain(cost(a)+cost(b)-1).append(page(listOf(a,b,small)))
        assertEquals(listOf(10L),c.items.map { it.id });assertEquals(10L,c.nextBeforeId)
        assertTrue(c.reachedPayloadLimit);assertFalse(c.canLoadMore)
    }
    @Test fun aggregateBudgetIncludesPreviousPages() {
        val a=row(10);val b=row(9);val c=row(8)
        val first=chain(cost(a)+cost(b)).append(page(listOf(a),true))
        val final=first.append(page(listOf(b,c)))
        assertEquals(cost(a),first.retainedPayloadBytes);assertEquals(cost(a)+cost(b),final.retainedPayloadBytes)
        assertEquals(listOf(10L,9L),final.items.map { it.id });assertEquals(9L,final.nextBeforeId)
        assertTrue(final.reachedPayloadLimit)
    }
    @Test fun malformedRowBeyondByteCutoffStillRejectsWholePage() {
        val a=row(10);val b=row(9);val first=chain(cost(a)+cost(b)).append(page(listOf(a),true))
        assertFailsWith<IllegalArgumentException> { first.append(page(listOf(b,row(8).copy(artifactId=9)))) }
        assertEquals(listOf(a),first.items);assertEquals(cost(a),first.retainedPayloadBytes)
    }
    @Test fun exhaustedChainCannotResumePastOmittedRows() {
        val a=row(10);val c=chain(cost(a)).append(page(listOf(a,row(9))))
        assertFailsWith<IllegalArgumentException> { c.append(page(listOf(row(8)))) }
    }
    @Test fun defaultFourMiBCapAppliesToRealLargeCitationModels() {
        assertEquals(4L*1024*1024,ARTIFACT_HISTORY_PAYLOAD_BYTES)
        val rows=(10L downTo 6L).map { row(it,"x".repeat(1024*1024-400)) }
        val c=chain().append(page(rows))
        assertEquals(4,c.items.size);assertTrue(c.reachedPayloadLimit)
        assertEquals(rows.take(4).sumOf(::cost),c.retainedPayloadBytes)
        assertTrue(c.retainedPayloadBytes<=ARTIFACT_HISTORY_PAYLOAD_BYTES)
    }
    @Test fun freshChainResetsPreviouslyExhaustedBudget() {
        val a=row(10);val exhausted=chain(cost(a)).append(page(listOf(a,row(9))))
        val fresh=chain(cost(a)+100).append(page(listOf(a),true))
        assertTrue(exhausted.reachedPayloadLimit);assertFalse(fresh.reachedPayloadLimit);assertTrue(fresh.canLoadMore)
    }
    @Test fun measuredCostOverflowCannotWrapAndAdmitHugeRow() {
        val c=ArtifactPageChain<Citation>(artifactId=7,limit=100,idOf={it.id},artifactIdOf={it.artifactId},rowSizeOf={Long.MAX_VALUE})
        val final=c.append(page(listOf(row(10))))
        assertTrue(final.items.isEmpty());assertEquals(0L,final.retainedPayloadBytes);assertTrue(final.reachedPayloadLimit)
    }
    @Test fun negativeMeasuredCostCannotIncreaseAvailableBudget() {
        val c=ArtifactPageChain<Citation>(artifactId=7,limit=100,idOf={it.id},artifactIdOf={it.artifactId},rowSizeOf={-1})
        assertFailsWith<IllegalArgumentException> { c.append(page(listOf(row(10)))) }
    }
    @Test fun zeroMeasuredCostCannotBypassEnabledBudget() {
        val c=ArtifactPageChain<Citation>(artifactId=7,limit=100,idOf={it.id},artifactIdOf={it.artifactId},rowSizeOf={0})
        assertFailsWith<IllegalArgumentException> { c.append(page(listOf(row(10)))) }
    }

}
