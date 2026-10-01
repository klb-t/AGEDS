package dev.klbt.ageds.core

import kotlin.test.*

/** Adversarial server envelopes; no transport cancellation assumptions. */
class ArtifactPageAdversarialTest {
    private data class Item(val id:Long,val artifact:Long=7)
    private fun chain(limit:Int=2,maxItems:Int=1000)=ArtifactPageChain<Item>(artifactId=7,limit=limit,maxItems=maxItems,idOf={it.id},artifactIdOf={it.artifact})
    private fun page(ids:List<Long>,more:Boolean=false,next:Long?=null,snapshot:Long=10,artifact:Long=7,limit:Int=2)=
        ArtifactPage(artifactId=artifact,items=ids.map { Item(it) },nextBeforeId=next,snapshotMaxId=snapshot,hasMore=more,limit=limit)

    @Test fun validPagesKeepDescendingSnapshotAndDoNotMutatePriorChain() {
        val initial=chain();val first=initial.append(page(listOf(10,8),true,8));val final=first.append(page(listOf(7,2)))
        assertTrue(initial.items.isEmpty());assertEquals(listOf(10L,8L),first.items.map { it.id })
        assertEquals(listOf(10L,8L,7L,2L),final.items.map { it.id });assertEquals(10L,final.snapshotMaxId)
        assertFalse(final.hasMore);assertFalse(final.canLoadMore)
    }
    @Test fun sameIdTwiceWithinPageIsRejected() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(8,8))) }
    }
    @Test fun ascendingOrNonpositiveIdsAreRejected() {
        for(ids in listOf(listOf(2L,3L),listOf(0L),listOf(-1L))) assertFailsWith<IllegalArgumentException>(ids.toString()) { chain().append(page(ids)) }
    }
    @Test fun envelopeAndItemArtifactMustMatchRequestedArtifact() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(8),artifact=9)) }
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(8)).copy(items=listOf(Item(8,9)))) }
    }
    @Test fun itemsNewerThanSnapshotAreRejected() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(11))) }
    }
    @Test fun snapshotCannotChangeBetweenPages() {
        val first=chain().append(page(listOf(10,8),true,8))
        assertFailsWith<IllegalArgumentException> { first.append(page(listOf(7),snapshot=11)) }
    }
    @Test fun duplicatedOrNewerIdAtNextPageIsRejected() {
        val first=chain().append(page(listOf(10,8),true,8))
        for(id in listOf(10L,9L,8L)) assertFailsWith<IllegalArgumentException>(id.toString()) { first.append(page(listOf(id))) }
        assertEquals(listOf(10L,8L),first.items.map { it.id })
    }
    @Test fun cursorMustMatchLastReturnedIdWhenMoreExists() {
        for(cursor in listOf(null,0L,7L,9L,Long.MAX_VALUE)) assertFailsWith<IllegalArgumentException>(cursor.toString()) { chain().append(page(listOf(10,8),true,cursor)) }
    }
    @Test fun terminalPageCannotAdvertiseAnotherCursor() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(8),false,8)) }
    }
    @Test fun emptyPageCannotClaimMoreRows() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(emptyList(),true,8)) }
    }
    @Test fun advertisedLimitMustRemainRequestedAndPageMustFit() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(8),limit=3)) }
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(10,9,8))) }
    }
    @Test fun completedChainRejectsUnexpectedExtraPage() {
        val terminal=chain().append(page(listOf(8)))
        assertFailsWith<IllegalArgumentException> { terminal.append(page(listOf(7))) }
    }
    @Test fun emptyArtifactSnapshotIsRepresentedWithoutInventedRows() {
        val empty=chain().append(page(emptyList(),snapshot=0))
        assertTrue(empty.items.isEmpty());assertEquals(0L,empty.snapshotMaxId);assertFalse(empty.canLoadMore)
    }
    @Test fun clientCapIsExplicitEvenWhenServerStillHasMore() {
        var current=chain(limit=100)
        repeat(10) { batch ->
            val ids=(2000L-batch*100 downTo 1901L-batch*100).toList()
            current=current.append(page(ids,true,ids.last(),snapshot=2000,limit=100))
        }
        assertEquals(1000,current.items.size);assertTrue(current.hasMore);assertTrue(current.reachedClientLimit);assertFalse(current.canLoadMore)
        assertFailsWith<IllegalArgumentException> { current.append(page(listOf(1),snapshot=2000,limit=100)) }
    }
    @Test fun callerMutationCannotAlterAcceptedPage() {
        val items=mutableListOf(Item(10),Item(8));val accepted=chain().append(page(emptyList(),true,8).copy(items=items))
        items.clear();assertEquals(listOf(10L,8L),accepted.items.map { it.id })
    }
    @Test fun shortNonemptyPageMayHaveMoreBecauseByteBudgetIsIndependent() {
        val first=chain().append(page(listOf(10),true,10))
        assertTrue(first.canLoadMore);assertEquals(10L,first.nextBeforeId)
    }
    @Test fun emptyContinuationCanEndPreviouslyNonemptySnapshot() {
        val first=chain().append(page(listOf(10),true,10))
        val terminal=first.append(page(emptyList()))
        assertEquals(listOf(10L),terminal.items.map { it.id });assertFalse(terminal.hasMore)
    }
    @Test fun initialEmptyPageCannotInventPositiveSnapshot() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(emptyList())) }
    }
    @Test fun nullableOwnerCannotBypassArtifactIsolation() {
        val c=ArtifactPageChain<Item>(artifactId=7,limit=2,maxItems=1000,idOf={it.id},artifactIdOf={null})
        assertFailsWith<IllegalArgumentException> { c.append(page(listOf(8))) }
    }

    @Test fun malformedItemsBeyondClientCapAreStillRejected() {
        val c=chain(maxItems=1)
        val forged=page(listOf(10,8)).copy(items=listOf(Item(10),Item(8,9)))
        assertFailsWith<IllegalArgumentException> { c.append(forged) };assertTrue(c.items.isEmpty())
    }
    @Test fun truncatedTerminalServerPageRemainsExplicitlyIncompleteLocally() {
        val c=chain(maxItems=1).append(page(listOf(10,8)))
        assertEquals(listOf(10L),c.items.map { it.id });assertTrue(c.hasMore);assertTrue(c.reachedClientLimit);assertFalse(c.canLoadMore)
    }
    @Test fun continuationBelowMinimumPositiveIdIsImpossible() {
        assertFailsWith<IllegalArgumentException> { chain().append(page(listOf(1),true,1)) }
    }

}
