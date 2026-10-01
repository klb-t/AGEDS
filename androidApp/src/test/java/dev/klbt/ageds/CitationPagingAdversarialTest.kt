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

/** Late pages deliberately ignore cancellation; envelopes capture origin and cursor. */
class CitationPagingAdversarialTest {
    private class Service : CitationService {
        data class Request(val kind:String,val artifact:Long,val before:Long?,val snapshot:Long?)
        val requests=mutableListOf<Request>()
        var holdVersions=false
        var holdCitations=false
        var holdAnnotations=false
        var wrongOlderCitation=false
        var fullPages=false
        var heldVersions:Continuation<ArtifactPage<TranscriptVersion>>?=null
        var heldCitations:Continuation<ArtifactPage<Citation>>?=null
        var heldAnnotations:Continuation<ArtifactPage<EvidenceAnnotation>>?=null
        var closed=0
        fun versions(id:Long,before:Long?=null,limit:Int=100):ArtifactPage<TranscriptVersion> {
            val top=if(fullPages) 2000L else 100L
            val start=before?.minus(1) ?: top
            val ids=(start downTo start-(if(fullPages) limit else 2)+1).toList()
            return ArtifactPage(id,ids.map { TranscriptVersion(it,id) },ids.last(),top,true,limit)
        }
        fun citationPage(id:Long,before:Long?=null,limit:Int=100):ArtifactPage<Citation> {
            val ids=if(before==null) listOf(500L,499L) else listOf(498L,497L)
            return ArtifactPage(id,ids.map { Citation(it,id,90,100,200,"raw-90","fixture",JsonObject(emptyMap())) },if(before==null) ids.last() else null,500,before==null,limit)
        }
        fun annotationPage(id:Long,before:Long?=null,limit:Int=100):ArtifactPage<EvidenceAnnotation> {
            val ids=if(before==null) listOf(900L,899L) else listOf(898L,897L)
            return ArtifactPage(id,ids.map { EvidenceAnnotation(it,id,body="note-$it",derivedTextId=90) },if(before==null) ids.last() else null,900,before==null,limit)
        }
        override suspend fun transcriptVersionsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?):ArtifactPage<TranscriptVersion> {
            requests.add(Request("versions",id,beforeId,snapshotMaxId))
            if(beforeId!=null && holdVersions) return suspendCoroutine { heldVersions=it }
            return versions(id,beforeId,limit)
        }
        override suspend fun citationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?):ArtifactPage<Citation> {
            requests.add(Request("citations",id,beforeId,snapshotMaxId))
            if(beforeId!=null && holdCitations) return suspendCoroutine { heldCitations=it }
            return citationPage(id,beforeId,limit).let { if(beforeId!=null && wrongOlderCitation) it.copy(artifactId=id+1) else it }
        }
        override suspend fun annotationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?):ArtifactPage<EvidenceAnnotation> {
            requests.add(Request("annotations",id,beforeId,snapshotMaxId))
            if(beforeId!=null && holdAnnotations) return suspendCoroutine { heldAnnotations=it }
            return annotationPage(id,beforeId,limit)
        }
        override suspend fun transcript(id:Long,versionId:Long)=Transcript(versionId,id,text="raw-$versionId",segments=listOf(TranscriptSegment(0.1,0.2,"raw-$versionId")))
        override suspend fun createCitation(id:Long,request:CitationCreate)=error("No writes in paging fixture")
        override suspend fun annotate(id:Long,request:AnnotationCreate)=error("No writes in paging fixture")
        override fun contentUrl(id:Long)="fixture/$id"
        override fun close() { closed++ }
    }
    private class Fixture {
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val servers=mutableMapOf<String,Service>()
        val workspace=CitationWorkspace(scope,{servers.getValue(it)},RangePlaybackController { error("Unexpected audio") })
        fun service(url:String)=Service().also { servers[url]=it }
        fun close() { workspace.clear();scope.cancel() }
    }

    @Test fun eachCollectionUsesItsOwnSnapshotAndCursor() {
        val f=Fixture();try {
            val s=f.service("a");f.workspace.open("a",7)
            f.workspace.loadOlderVersions();f.workspace.loadOlderCitations();f.workspace.loadOlderAnnotations()
            assertTrue(s.requests.contains(Service.Request("versions",7,99,100)))
            assertTrue(s.requests.contains(Service.Request("citations",7,499,500)))
            assertTrue(s.requests.contains(Service.Request("annotations",7,899,900)))
            assertEquals(listOf(100L,99L,98L,97L),f.workspace.versions.map { it.id })
            assertEquals(listOf(500L,499L,498L,497L),f.workspace.citations.map { it.id })
            assertEquals(listOf(900L,899L,898L,897L),f.workspace.annotations.map { it.id })
        } finally { f.close() }
    }
    @Test fun lateVersionPageAfterServerAndArtifactChangeCannotMixLists() {
        val f=Fixture();try {
            val a=f.service("a");f.service("b");f.workspace.open("a",7);a.holdVersions=true;f.workspace.loadOlderVersions()
            f.workspace.open("b",9);a.heldVersions!!.resume(a.versions(7,99))
            assertEquals(listOf(100L,99L),f.workspace.versions.map { it.id });assertTrue(f.workspace.versions.all { it.artifactId==9L })
            assertEquals(9L,f.workspace.transcript.value!!.artifactId);assertEquals(1,a.closed)
        } finally { f.close() }
    }
    @Test fun lateCitationPageAfterSameServerArtifactChangeCannotMixLists() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);a.holdCitations=true;f.workspace.loadOlderCitations()
            val old=a.heldCitations!!;a.holdCitations=false;f.workspace.open("a",9);old.resume(a.citationPage(7,499))
            assertEquals(2,f.workspace.citations.size);assertTrue(f.workspace.citations.all { it.artifactId==9L })
        } finally { f.close() }
    }
    @Test fun lateAnnotationPageAfterDisposalDoesNotPublish() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);a.holdAnnotations=true;f.workspace.loadOlderAnnotations()
            f.workspace.deactivate();a.heldAnnotations!!.resume(a.annotationPage(7,899))
            assertEquals(listOf(900L,899L),f.workspace.annotations.map { it.id })
        } finally { f.close() }
    }
    @Test fun latePageAfterClearCannotRestoreAbandonedArtifact() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);a.holdVersions=true;f.workspace.loadOlderVersions()
            f.workspace.clear();a.heldVersions!!.resume(a.versions(7,99))
            assertTrue(f.workspace.versions.isEmpty());assertNull(f.workspace.transcript.value);assertNull(f.workspace.preview.value)
        } finally { f.close() }
    }
    @Test fun versionChangeWhileLoadingOlderCitationsPreservesNewPinnedSelection() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);a.holdCitations=true;f.workspace.loadOlderCitations()
            f.workspace.load(99);f.workspace.select(listOf(0));val preview=f.workspace.preview.value!!
            a.heldCitations!!.resume(a.citationPage(7,499))
            assertEquals(99L,f.workspace.transcript.value!!.id);assertSame(preview,f.workspace.preview.value)
            assertEquals("raw-99",f.workspace.preview.value!!.quoteText)
            assertEquals(listOf(500L,499L),f.workspace.citations.map { it.id })
            assertFalse(f.workspace.citationPages.loading.value)
            a.holdCitations=false;f.workspace.loadOlderCitations()
            assertEquals(listOf(500L,499L,498L,497L),f.workspace.citations.map { it.id })
            assertSame(preview,f.workspace.preview.value)
        } finally { f.close() }
    }
    @Test fun versionChangeRetainsAlreadyLoadedPagesAndDoesNotRefetchThem() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);f.workspace.loadOlderVersions();f.workspace.loadOlderCitations()
            val before=a.requests.size;f.workspace.load(98)
            assertEquals(before,a.requests.size);assertEquals(4,f.workspace.versions.size);assertEquals(4,f.workspace.citations.size)
            assertEquals(98L,f.workspace.transcript.value!!.id)
        } finally { f.close() }
    }
    @Test fun malformedOlderPageIsRejectedAtomicallyWithoutReplacingPinnedQuote() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);f.workspace.select(listOf(0));val selected=f.workspace.preview.value
            a.wrongOlderCitation=true;f.workspace.loadOlderCitations()
            assertEquals(listOf(500L,499L),f.workspace.citations.map { it.id });assertSame(selected,f.workspace.preview.value)
            assertNotNull(f.workspace.citationPages.error.value);assertFalse(f.workspace.citationPages.loading.value)
        } finally { f.close() }
    }
    @Test fun repeatedLoadOlderWhilePendingDoesNotDuplicateRequest() {
        val f=Fixture();try {
            val a=f.service("a");f.workspace.open("a",7);a.holdVersions=true
            f.workspace.loadOlderVersions();f.workspace.loadOlderVersions()
            assertEquals(1,a.requests.count { it.kind=="versions" && it.before!=null })
            a.heldVersions!!.resume(a.versions(7,99));assertEquals(4,f.workspace.versions.size)
        } finally { f.close() }
    }
    @Test fun thousandItemCapStopsNetworkRequestsWithoutClaimingFullCoverage() {
        val f=Fixture();try {
            val a=f.service("a").apply { fullPages=true };f.workspace.open("a",7)
            repeat(12) { f.workspace.loadOlderVersions() }
            assertEquals(1000,f.workspace.versions.size)
            assertEquals(10,a.requests.count { it.kind=="versions" })
            assertFalse(f.workspace.versionPages.canLoadMore.value)
            assertTrue(f.workspace.versionPages.coverage.value.contains("1000"))
            assertTrue(f.workspace.versionPages.coverage.value.contains("limit"))
        } finally { f.close() }
    }
}
