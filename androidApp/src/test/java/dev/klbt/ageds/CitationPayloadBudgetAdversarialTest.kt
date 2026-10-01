package dev.klbt.ageds

import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
import kotlinx.coroutines.*
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.*
import org.junit.Test
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/** Real CitationWorkspace serializers with non-cooperative late page callbacks. */
class CitationPayloadBudgetAdversarialTest {
    private class Service : CitationService {
        var large=false
        var oversized=false
        var held:Continuation<ArtifactPage<Citation>>?=null
        var holdOlder=false
        var saved:Citation?=null
        var citationRequests=0
        fun citation(id:Long,text:String)=Citation(id,7,41,100,200,text,"fixture",JsonObject(emptyMap()))
        fun citationPage(before:Long?=null):ArtifactPage<Citation> {
            saved?.let { return ArtifactPage(7,listOf(it),null,it.id,false,100) }
            val ids=if(before==null) listOf(100L,99L,98L) else listOf(97L,96L)
            val text=if(oversized) "x".repeat(4*1024*1024) else if(large) "x".repeat(1024*1024-400) else "ą🐈\"\n"
            return ArtifactPage(7,ids.map { citation(it,text) },if(before==null) ids.last() else null,100,before==null,100)
        }
        override suspend fun transcriptVersionsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?)=
            ArtifactPage(id,listOf(TranscriptVersion(41,id,model="ą🐈\"\n")),null,41,false,limit)
        override suspend fun transcript(id:Long,versionId:Long)=Transcript(versionId,id,text="raw",segments=listOf(TranscriptSegment(0.1,0.2,"raw")))
        override suspend fun citationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?):ArtifactPage<Citation> {
            citationRequests++
            if(beforeId!=null && holdOlder) return suspendCoroutine { held=it }
            return citationPage(beforeId)
        }
        override suspend fun annotationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?)=
            ArtifactPage(id,listOf(EvidenceAnnotation(51,id,body="ą🐈\"\n",derivedTextId=41)),null,51,false,limit)
        override suspend fun createCitation(id:Long,request:CitationCreate)=citation(101,request.quoteText!!).also { saved=it }
        override suspend fun annotate(id:Long,request:AnnotationCreate)=error("No annotation writes")
        override fun contentUrl(id:Long)="fixture/$id"
        override fun close() {}
    }
    private class Fixture {
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val service=Service()
        val workspace=CitationWorkspace(scope,{service},RangePlaybackController { error("No decoder expected") })
        fun open()=workspace.open("fixture",7)
        fun close() { workspace.clear();scope.cancel() }
    }
    private fun citationBytes(rows:List<Citation>)=rows.sumOf { serializedHistoryRowBytes(Citation.serializer(),it) }

    @Test fun allThreeProductionCollectionsUseRealSerializedUtf8ModelCosts() {
        val f=Fixture();try {
            f.open();val w=f.workspace
            assertEquals(w.versions.sumOf { serializedHistoryRowBytes(TranscriptVersion.serializer(),it) },w.versionPages.retainedPayloadBytes.value)
            assertEquals(citationBytes(w.citations),w.citationPages.retainedPayloadBytes.value)
            assertEquals(w.annotations.sumOf { serializedHistoryRowBytes(EvidenceAnnotation.serializer(),it) },w.annotationPages.retainedPayloadBytes.value)
            assertTrue(w.citationPages.retainedPayloadBytes.value>0)
        } finally { f.close() }
    }
    @Test fun realMultiPageLargeQuotesStopAtFourMiBAndDoNotIssueAnotherFetch() {
        val f=Fixture();try {
            f.service.large=true;f.open();f.workspace.loadOlderCitations();val w=f.workspace
            assertEquals(listOf(100L,99L,98L,97L),w.citations.map { it.id })
            assertEquals(citationBytes(w.citations),w.citationPages.retainedPayloadBytes.value)
            assertTrue(w.citationPages.retainedPayloadBytes.value<=ARTIFACT_HISTORY_PAYLOAD_BYTES)
            assertTrue(w.citationPages.reachedPayloadLimit.value);assertFalse(w.citationPages.canLoadMore.value)
            assertTrue(w.citationPages.coverage.value.contains("4 MiB"))
            val count=f.service.citationRequests;w.loadOlderCitations();assertEquals(count,f.service.citationRequests)
        } finally { f.close() }
    }
    @Test fun oversizedFirstRowLeavesNoFabricatedCompleteList() {
        val f=Fixture();try {
            f.service.oversized=true;f.open();val pages=f.workspace.citationPages
            assertTrue(pages.items.isEmpty());assertEquals(0L,pages.retainedPayloadBytes.value)
            assertTrue(pages.reachedPayloadLimit.value);assertFalse(pages.canLoadMore.value)
            assertTrue(pages.coverage.value.contains("pominięto"))
        } finally { f.close() }
    }
    @Test fun refreshResetsExhaustedBudgetAndRetainsPinnedTranscriptVersion() {
        val f=Fixture();try {
            f.service.large=true;f.open();f.workspace.loadOlderCitations();assertTrue(f.workspace.citationPages.reachedPayloadLimit.value)
            f.service.large=false;f.workspace.refresh()
            assertFalse(f.workspace.citationPages.reachedPayloadLimit.value);assertTrue(f.workspace.citationPages.canLoadMore.value)
            assertEquals(41L,f.workspace.transcript.value!!.id)
            assertEquals(citationBytes(f.workspace.citations),f.workspace.citationPages.retainedPayloadBytes.value)
        } finally { f.close() }
    }
    @Test fun lateLargePageAfterRefreshCannotConsumeNewSnapshotBudget() {
        val f=Fixture();try {
            f.service.large=true;f.open();f.service.holdOlder=true;f.workspace.loadOlderCitations()
            val late=f.service.citationPage(98);val callback=f.service.held!!
            f.service.large=false;f.workspace.refresh();val bytes=f.workspace.citationPages.retainedPayloadBytes.value
            callback.resume(late)
            assertEquals(3,f.workspace.citations.size);assertEquals(bytes,f.workspace.citationPages.retainedPayloadBytes.value)
            assertFalse(f.workspace.citationPages.reachedPayloadLimit.value)
        } finally { f.close() }
    }
    @Test fun saveRefreshDuringOlderRequestCannotRestoreOldLargePayload() {
        val f=Fixture();try {
            f.service.large=true;f.open();f.workspace.select(listOf(0));f.service.holdOlder=true;f.workspace.loadOlderCitations()
            val late=f.service.citationPage(98);val callback=f.service.held!!;f.workspace.save();callback.resume(late)
            assertEquals(listOf(101L),f.workspace.citations.map { it.id })
            assertEquals("raw",f.workspace.citations.single().quoteText)
            assertEquals(citationBytes(f.workspace.citations),f.workspace.citationPages.retainedPayloadBytes.value)
            assertFalse(f.workspace.citationPages.reachedPayloadLimit.value)
        } finally { f.close() }
    }
    @Test fun disposalRejectsLateBudgetChargeAsWellAsLateRows() {
        val f=Fixture();try {
            f.service.large=true;f.open();f.service.holdOlder=true;f.workspace.loadOlderCitations()
            val bytes=f.workspace.citationPages.retainedPayloadBytes.value
            f.workspace.deactivate();f.service.held!!.resume(f.service.citationPage(98))
            assertEquals(3,f.workspace.citations.size);assertEquals(bytes,f.workspace.citationPages.retainedPayloadBytes.value)
            assertFalse(f.workspace.citationPages.reachedPayloadLimit.value)
        } finally { f.close() }
    }
}
