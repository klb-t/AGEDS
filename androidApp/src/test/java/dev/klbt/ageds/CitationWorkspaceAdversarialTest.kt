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

/** Deliberately non-cooperative transport: cancellation does not suppress a late response. */
class CitationWorkspaceAdversarialTest {
    private class Service : CitationService {
        var heldRead: Continuation<Transcript?>? = null
        var heldSave: Continuation<Citation>? = null
        var delayRead = false
        var delaySave = false
        var wrongVersion = false
        var wrongSave = false
        var closed = 0
        val requests = mutableListOf<Pair<Long,CitationCreate>>()
        fun text(id:Long, version:Long) = Transcript(version,id,text="raw-$version",segments=listOf(TranscriptSegment(0.1,0.2,"raw-$version")))
        fun saved(id:Long, request:CitationCreate) = Citation(3,id,request.derivedTextId,100,200,request.quoteText!!,"fixture",JsonObject(emptyMap()))
        override suspend fun transcriptVersionsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?) = ArtifactPage(id,listOf(TranscriptVersion(41,id),TranscriptVersion(40,id)),null,41,false,limit)
        override suspend fun transcript(id:Long,versionId:Long):Transcript? {
            if(delayRead) return suspendCoroutine { heldRead=it }
            return text(id,if(wrongVersion) 99 else versionId)
        }
        override suspend fun citationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?)=ArtifactPage<Citation>(id,emptyList(),null,0,false,limit)
        override suspend fun annotationsPage(id:Long,limit:Int,beforeId:Long?,snapshotMaxId:Long?)=ArtifactPage<EvidenceAnnotation>(id,emptyList(),null,0,false,limit)
        override suspend fun createCitation(id:Long,request:CitationCreate):Citation {
            requests.add(id to request)
            if(delaySave) return suspendCoroutine { heldSave=it }
            return saved(id,request).let { if(wrongSave) it.copy(derivedTextId=99) else it }
        }
        override suspend fun annotate(id:Long,request:AnnotationCreate)=EvidenceAnnotation(1,id,body=request.body,derivedTextId=request.derivedTextId)
        override fun contentUrl(id:Long)="fixture/$id"
        override fun close() { closed++ }
    }
    private class Fixture {
        val scope=CoroutineScope(SupervisorJob()+Dispatchers.Unconfined)
        val servers=mutableMapOf<String,Service>()
        val workspace=CitationWorkspace(scope,{servers.getValue(it)},RangePlaybackController { error("Unexpected decoder") })
        fun service(url:String)=Service().also { servers[url]=it }
        fun close() { workspace.clear(); scope.cancel() }
    }

    @Test fun staleFetchAfterServerAndArtifactChangeCannotReplaceCurrentVersion() {
        val f=Fixture()
        try {
            val a=f.service("a").apply { delayRead=true }; f.workspace.open("a",7)
            val b=f.service("b"); f.workspace.open("b",9)
            assertEquals(1,a.closed)
            a.heldRead!!.resume(a.text(7,41))
            assertEquals(9L,f.workspace.transcript.value!!.artifactId)
            assertEquals(b.text(9,41),f.workspace.transcript.value)
            assertFalse(f.workspace.busy.value); assertNull(f.workspace.error.value)
        } finally { f.close() }
    }
    @Test fun staleVersionFetchCannotReplaceNewerSelection() {
        val f=Fixture()
        try {
            val a=f.service("a").apply { delayRead=true }; f.workspace.open("a",7)
            val old=a.heldRead!!; a.delayRead=false; f.workspace.load(40)
            old.resume(a.text(7,41))
            assertEquals(40L,f.workspace.transcript.value!!.id)
            f.workspace.select(listOf(0)); assertEquals(40L,f.workspace.preview.value!!.derivedTextId)
        } finally { f.close() }
    }
    @Test fun lateSaveAfterServerChangeCannotInsertOldCitation() {
        val f=Fixture()
        try {
            val a=f.service("a"); f.service("b"); f.workspace.open("a",7); f.workspace.select(listOf(0))
            a.delaySave=true; f.workspace.save(); val request=a.requests.single()
            f.workspace.open("b",9); a.heldSave!!.resume(a.saved(request.first,request.second))
            assertTrue(f.workspace.citations.isEmpty()); assertNull(f.workspace.message.value)
            assertEquals(9L,f.workspace.transcript.value!!.artifactId); assertFalse(f.workspace.busy.value)
        } finally { f.close() }
    }
    @Test fun lateSaveAfterVersionChangeCannotPublishInNewView() {
        val f=Fixture()
        try {
            val a=f.service("a"); f.workspace.open("a",7); f.workspace.select(listOf(0)); a.delaySave=true
            f.workspace.save(); val request=a.requests.single(); f.workspace.load(40)
            a.heldSave!!.resume(a.saved(request.first,request.second))
            assertEquals(41L,request.second.derivedTextId); assertEquals("raw-41",request.second.quoteText)
            assertEquals(40L,f.workspace.transcript.value!!.id); assertTrue(f.workspace.citations.isEmpty())
        } finally { f.close() }
    }
    @Test fun disposalSuppressesLateFetchAndReleasesService() {
        val f=Fixture()
        try {
            val a=f.service("a").apply { delayRead=true }; f.workspace.open("a",7); f.workspace.clear()
            a.heldRead!!.resume(a.text(7,41))
            assertEquals(1,a.closed); assertNull(f.workspace.transcript.value); assertTrue(f.workspace.versions.isEmpty())
            assertFalse(f.workspace.busy.value)
        } finally { f.close() }
    }
    @Test fun mixedVersionResponseIsRejectedBeforeSelection() {
        val f=Fixture()
        try {
            f.service("a").wrongVersion=true; f.workspace.open("a",7)
            assertNull(f.workspace.transcript.value); assertNotNull(f.workspace.error.value)
            f.workspace.select(listOf(0)); assertNull(f.workspace.preview.value)
        } finally { f.close() }
    }
    @Test fun mismatchedSaveResponseCannotBecomeVisibleCitation() {
        val f=Fixture()
        try {
            val a=f.service("a"); f.workspace.open("a",7); f.workspace.select(listOf(0)); a.wrongSave=true
            f.workspace.save()
            assertTrue(f.workspace.citations.isEmpty()); assertNotNull(f.workspace.error.value); assertNull(f.workspace.message.value)
        } finally { f.close() }
    }
}
