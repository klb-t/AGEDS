package dev.klbt.ageds

import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.Before
import org.junit.After
import androidx.test.filters.SdkSuppress
import android.os.Bundle
import org.junit.runner.RunWith
import java.io.File

/** Compile-checked device contract; execution requires an actual Android runtime. */
@RunWith(AndroidJUnit4::class)
@SdkSuppress(minSdkVersion = 29)
class SourceScannerProviderTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val target = instrumentation.targetContext
    private val resolver = instrumentation.context.contentResolver
    private lateinit var original: Bundle
    @Before fun grantSyntheticTreesAndRecordBytes() {
        // Bootstrap protected test provider without a picker; provider grants only its own fixtures.
        instrumentation.uiAutomation.adoptShellPermissionIdentity("android.permission.MANAGE_DOCUMENTS")
        try { original = snapshot() } finally { instrumentation.uiAutomation.dropShellPermissionIdentity() }
    }
    @After fun everyScenarioLeavesSourceBytesUnchanged() {
        val after = snapshot()
        assertEquals(original.getString("a"), after.getString("a"))
        assertEquals(original.getString("b"), after.getString("b"))
        assertEquals(original.getInt("writeAttempts"), after.getInt("writeAttempts"))
    }
    private fun snapshot() = resolver.call(SyntheticDocumentsProvider.tree("stats"), "fixtureSnapshot", null, null)!!
    private suspend fun scan(root: String) = SourceScanner(target).scan(SyntheticDocumentsProvider.tree(root))

    @Test fun seedlessScanPreservesDistinctUrisRawCellsAndSourceBytes() = runBlocking {
        val before = snapshot() // Starts test provider and grants synthetic read-only trees.
        val result = scan("normal")
        assertEquals(2, result.files.size)
        assertEquals(2, result.files.map { it.uri }.toSet().size)
        assertTrue(result.issues.any { it.code == "name_collision" })
        assertTrue(result.files.flatMap { it.rows }.flatMap { it.cells }.any { it.raw == "not-a-date" })
        result.files.forEach { file ->
            val id = android.provider.DocumentsContract.getDocumentId(android.net.Uri.parse(file.uri))
            assertEquals(before.getString(id), file.sha256)
        }
        val after = snapshot()
        assertEquals(before.getString("a"), after.getString("a"))
        assertEquals(before.getString("b"), after.getString("b"))
        assertEquals(before.getInt("writeAttempts"), after.getInt("writeAttempts"))
    }
    @Test fun deniedDirectoryIsExplicitlyPartial() = runBlocking {
        snapshot()
        val result = scan("denied")
        assertEquals("partial", result.coverage)
        assertTrue(result.files.isEmpty())
        assertTrue(result.issues.any { it.code == "directory_unreadable" })
    }
    @Test fun partialCursorRetainsEarlierRowsAndReportsFailure() = runBlocking {
        snapshot()
        val result = scan("partial")
        assertEquals(1, result.files.size)
        assertEquals("partial", result.coverage)
        assertTrue(result.issues.any { it.code == "directory_unreadable" })
    }
    @Test fun repeatedDocumentIdDoesNotReadTwice() = runBlocking {
        val before = snapshot()
        val result = scan("repeat")
        assertEquals(2, result.files.size)
        assertTrue(result.issues.any { it.code == "repeated_document" })
        assertEquals(2, snapshot().getInt("reads") - before.getInt("reads"))
    }
    @Test fun privateCacheRoundTripsWithoutChangingSources() = runBlocking {
        val before = snapshot()
        val directory = File(target.cacheDir, "provider-cache-${System.nanoTime()}").apply { mkdirs() }
        try {
            val isolated = object : ContextWrapper(target) { override fun getFilesDir(): File = directory }
            val cache = SourceScanCache(isolated)
            val result = scan("normal")
            cache.write(result) {}
            assertEquals(result, cache.read())
            assertEquals(before.getString("a"), snapshot().getString("a"))
            assertEquals(before.getInt("writeAttempts"), snapshot().getInt("writeAttempts"))
        } finally { directory.deleteRecursively() }
    }
    @Test fun cancelledProviderScanCannotReplacePriorCache() = runBlocking {
        val before = snapshot()
        val directory = File(target.cacheDir, "provider-cancel-${System.nanoTime()}").apply { mkdirs() }
        try {
            val isolated = object : ContextWrapper(target) { override fun getFilesDir(): File = directory }
            val cache = SourceScanCache(isolated)
            val baseline = scan("normal")
            cache.write(baseline) {}
            val job = launch(Dispatchers.Default) {
                val result = scan("slow")
                cache.write(result) { ensureActive() }
            }
            withTimeout(5000) {
                while (snapshot().getInt("slowQueries") == before.getInt("slowQueries")) delay(10)
            }
            job.cancelAndJoin()
            assertTrue(job.isCancelled)
            assertEquals(baseline, cache.read())
            assertEquals(before.getInt("writeAttempts"), snapshot().getInt("writeAttempts"))
        } finally { directory.deleteRecursively() }
    }
}
