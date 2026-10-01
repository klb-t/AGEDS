package dev.klbt.ageds

import dev.klbt.ageds.core.SourceScanLimits
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.CancellationException

/** The production scan engine is exercised through its read-only provider seam. */
class SourceScanEngineLifecycleTest {
    private class Cursor(private val documents:List<SourceScanDocument>):SourceScanCursor {
        var index=0
        var closes=0
        var failAt:Int?=null
        var closeFailure:Exception?=null
        var nextFailure:Exception=IOException("cursor next failed")
        var afterNext:(()->Unit)?=null
        override fun next():SourceScanDocument? {
            if(failAt==index) throw nextFailure
            val value=documents.getOrNull(index++)
            afterNext?.invoke()
            return value
        }
        override fun close() { closes++;closeFailure?.let { throw it } }
    }
    private class Stream(private val bytes:ByteArray=byteArrayOf(1,2,3,4),private val chunk:Int=2):InputStream() {
        var consumed=0
        var closes=0
        var reads=0
        var failAfter:Int?=null
        var zero=false
        var readFailure:Exception=IOException("stream read failed")
        var closeFailure:Exception?=null
        var afterRead:(()->Unit)?=null
        override fun read():Int=error("production should use bounded bulk reads")
        override fun read(buffer:ByteArray,offset:Int,length:Int):Int {
            reads++
            if(failAfter?.let { consumed>=it }==true) throw readFailure
            if(zero) return 0
            if(consumed==bytes.size) return -1
            val count=minOf(chunk,length,bytes.size-consumed)
            bytes.copyInto(buffer,offset,consumed,consumed+count);consumed+=count
            afterRead?.invoke();return count
        }
        override fun close() { closes++;closeFailure?.let { throw it } }
    }
    private class Provider(val root:Cursor):SourceScanProvider {
        override val rootId="root"
        override val rootUri="fake://root"
        val cursors=mutableMapOf("root" to root)
        val streams=mutableMapOf<String,Stream>()
        val childFailures=mutableMapOf<String,Exception>()
        val openFailures=mutableMapOf<String,Exception>()
        val opens=mutableListOf<String>()
        override fun uri(id:String)="fake://$id"
        override fun children(id:String):SourceScanCursor { childFailures[id]?.let { throw it };return cursors.getValue(id) }
        override fun openRead(id:String):InputStream { opens.add(id);openFailures[id]?.let { throw it };return streams[id] ?: throw IllegalStateException("Provider returned no content stream") }
    }
    private fun file(id:String="a",name:String="a.bin",size:Long?=null)=SourceScanDocument(id,name,"application/octet-stream",size)
    private fun dir(id:String)=SourceScanDocument(id,id,SOURCE_DIRECTORY_MIME,null)
    private fun scan(p:Provider,limits:SourceScanLimits=SourceScanLimits(),check:()->Unit={})=
        SourceScanEngine(p).scan(limits,"synthetic-time",check)
    private fun cancelled(block:()->Unit) {
        try { block();fail("cancellation must propagate instead of returning partial success") }
        catch(_:CancellationException) {}
    }

    @Test fun normalCompletionClosesEveryCursorAndInputOnce() {
        val cursor=Cursor(listOf(file()));val p=Provider(cursor);val stream=Stream();p.streams["a"]=stream
        val result=scan(p)
        assertEquals(1,cursor.closes);assertEquals(1,stream.closes);assertEquals(4L,result.bytesRead)
        assertNotNull(result.files.single().sha256)
    }
    @Test fun entryCapClosesLazyCursorBeforeProcessingAdmittedFiles() {
        val cursor=Cursor(listOf(file("a"),file("b"),file("c")));val p=Provider(cursor)
        p.streams["a"]=Stream();p.streams["b"]=Stream();p.streams["c"]=Stream()
        val result=scan(p,SourceScanLimits(maxFiles=1,maxDirectories=1))
        assertEquals(1,cursor.closes);assertTrue(result.issues.any { it.code=="entry_limit" || it.code=="file_limit" })
        assertEquals(1,p.opens.size);assertEquals(1,p.streams.getValue(p.opens.single()).closes)
    }
    @Test fun cursorIterationErrorClosesCursorAndPreservesAdmittedWorkAsPartial() {
        val cursor=Cursor(listOf(file())).apply { failAt=1 };val p=Provider(cursor);p.streams["a"]=Stream()
        val result=scan(p)
        assertEquals(1,cursor.closes);assertEquals(1,p.streams.getValue("a").closes)
        assertEquals("partial",result.coverage);assertTrue(result.issues.any { it.code=="directory_unreadable" })
    }
    @Test fun rootDirectoryErrorIsPartialWithExplicitDiagnostic() {
        val p=Provider(Cursor(emptyList()));p.childFailures["root"]=IOException("root denied")
        val result=scan(p)
        assertTrue(result.files.isEmpty());assertEquals("partial",result.coverage)
        assertTrue(result.issues.any { it.code=="directory_unreadable" });assertTrue(p.opens.isEmpty())
    }
    @Test fun childDirectoryErrorDoesNotLeakParentCursorOrHideSiblingFile() {
        val root=Cursor(listOf(dir("d"),file()));val p=Provider(root);p.childFailures["d"]=IOException("child denied");p.streams["a"]=Stream()
        val result=scan(p)
        assertEquals(1,root.closes);assertEquals(1,p.streams.getValue("a").closes)
        assertEquals(1,result.files.size);assertEquals("partial",result.coverage)
    }
    @Test fun cancellationFromDirectoryQueryIsNotConvertedToDirectoryError() {
        val p=Provider(Cursor(emptyList()));p.childFailures["root"]=CancellationException("query cancelled")
        cancelled { scan(p) };assertTrue(p.opens.isEmpty())
    }
    @Test fun cancellationFromLazyCursorClosesItAndDoesNotOpenFiles() {
        val cursor=Cursor(listOf(file())).apply { failAt=0;nextFailure=CancellationException("cursor cancelled") };val p=Provider(cursor)
        cancelled { scan(p) };assertEquals(1,cursor.closes);assertTrue(p.opens.isEmpty())
    }
    @Test fun cancellationCheckAfterCursorEmissionStillClosesCursor() {
        var stop=false;val cursor=Cursor(listOf(file())).apply { afterNext={stop=true} };val p=Provider(cursor)
        cancelled { scan(p) { if(stop) throw CancellationException("after next") } }
        assertEquals(1,cursor.closes);assertTrue(p.opens.isEmpty())
    }
    @Test fun cancellationFromInputReadClosesInputAndPriorCursor() {
        val cursor=Cursor(listOf(file()));val p=Provider(cursor);val stream=Stream().apply { failAfter=2;readFailure=CancellationException("read cancelled") };p.streams["a"]=stream
        cancelled { scan(p) };assertEquals(1,cursor.closes);assertEquals(1,stream.closes);assertEquals(2,stream.consumed)
    }
    @Test fun cancellationCheckAfterReturnedBytesClosesInput() {
        var stop=false;val cursor=Cursor(listOf(file()));val p=Provider(cursor);val stream=Stream().apply { afterRead={stop=true} };p.streams["a"]=stream
        cancelled { scan(p) { if(stop) throw CancellationException("after bytes") } }
        assertEquals(1,stream.closes);assertEquals(1,cursor.closes)
    }
    @Test fun cancellationThrownByCursorCloseIsNotSwallowed() {
        val cursor=Cursor(emptyList()).apply { closeFailure=CancellationException("close cancelled") };val p=Provider(cursor)
        cancelled { scan(p) };assertEquals(1,cursor.closes)
    }
    @Test fun cancellationThrownByInputCloseIsNotSwallowed() {
        val p=Provider(Cursor(listOf(file())));val stream=Stream().apply { closeFailure=CancellationException("close cancelled") };p.streams["a"]=stream
        cancelled { scan(p) };assertEquals(1,stream.closes)
    }
    @Test fun missingInputProducesUnreadableFileWithoutInventedHash() {
        val p=Provider(Cursor(listOf(file())));val result=scan(p);val f=result.files.single()
        assertNull(f.sha256);assertEquals("unreadable",f.coverage);assertTrue(f.issues.any { it.code=="read_failed" });assertEquals(0L,result.bytesRead)
    }
    @Test fun zeroProgressFailsPromptlyClosesAndWithholdsHash() {
        val p=Provider(Cursor(listOf(file())));val stream=Stream().apply { zero=true };p.streams["a"]=stream
        val result=scan(p);assertEquals(1,stream.reads);assertEquals(1,stream.closes);assertEquals(0L,result.bytesRead)
        assertEquals("unreadable",result.files.single().coverage);assertNull(result.files.single().sha256)
    }
    @Test fun partialNonWavReadFailureRemainsChargedAndNoHashIsPublished() {
        val p=Provider(Cursor(listOf(file())));val stream=Stream().apply { failAfter=2 };p.streams["a"]=stream
        val result=scan(p);assertEquals(2L,result.bytesRead);assertEquals(1,stream.closes)
        assertEquals("unreadable",result.files.single().coverage);assertNull(result.files.single().sha256)
    }
    @Test fun partialWavReadFailureRemainsChargedAndNoHashIsPublished() {
        val p=Provider(Cursor(listOf(file(name="a.wav"))));val stream=Stream().apply { failAfter=2 };p.streams["a"]=stream
        val result=scan(p);assertEquals(2L,result.bytesRead);assertEquals(1,stream.closes)
        assertEquals("unreadable",result.files.single().coverage);assertNull(result.files.single().sha256)
    }
    @Test fun normalCursorCloseFailureIsExplicitAndRetainsAdmittedWork() {
        val cursor=Cursor(listOf(file())).apply { closeFailure=IOException("cursor close failed") };val p=Provider(cursor);p.streams["a"]=Stream()
        val result=scan(p);assertEquals(1,cursor.closes);assertEquals(1,result.files.size)
        assertEquals("partial",result.coverage);assertTrue(result.issues.any { it.code=="directory_unreadable" })
    }
    @Test fun normalInputCloseFailureWithholdsHashButPreservesReadCount() {
        val p=Provider(Cursor(listOf(file())));val stream=Stream().apply { closeFailure=IOException("input close failed") };p.streams["a"]=stream
        val result=scan(p);assertEquals(4L,result.bytesRead);assertEquals(1,stream.closes)
        assertNull(result.files.single().sha256);assertEquals("unreadable",result.files.single().coverage)
    }
    @Test fun byteBudgetStopClosesBothGenericAndWavInputs() {
        for(name in listOf("a.bin","a.wav")) {
            val p=Provider(Cursor(listOf(file(name=name))));val stream=Stream();p.streams["a"]=stream
            val result=scan(p,SourceScanLimits(maxFileBytes=2,maxTotalBytes=2))
            assertEquals(name,1,stream.closes);assertNull(result.files.single().sha256)
            assertEquals("partial",result.files.single().coverage)
        }
    }
    @Test fun cancellationFromOpenReadIsNotConvertedToUnreadableResult() {
        val cursor=Cursor(listOf(file()));val p=Provider(cursor);p.openFailures["a"]=CancellationException("open cancelled")
        cancelled { scan(p) };assertEquals(1,cursor.closes)
    }

}
