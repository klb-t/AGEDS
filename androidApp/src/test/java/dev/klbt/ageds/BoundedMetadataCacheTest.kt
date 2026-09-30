package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class BoundedMetadataCacheTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test fun replacementPreservesUnicodeAndLeavesNoTemporaryFiles() {
        val path = File(temporary.root, "scan.json")
        val cache = BoundedMetadataCache(path, 128)
        assertNull(cache.read())
        cache.write("{\"uri\":\"content://źródło/α\"}")
        assertEquals("{\"uri\":\"content://źródło/α\"}", cache.read())
        cache.write("{\"version\":2}")
        assertEquals("{\"version\":2}", cache.read())
        assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })
    }

    @Test fun oversizedReplacementDoesNotDestroyPreviousReceipt() {
        val path = File(temporary.root, "scan.json")
        val cache = BoundedMetadataCache(path, 12)
        cache.write("original")
        // Character count fits; UTF-8 byte count exceeds the budget.
        val error = runCatching { cache.write("ą".repeat(8)) }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
        assertEquals("original", cache.read())
    }

    @Test fun exactlyAtByteBudgetIsAccepted() {
        val cache = BoundedMetadataCache(File(temporary.root, "scan.json"), 8)
        cache.write("ą".repeat(4))
        assertEquals("ą".repeat(4), cache.read())
    }

    @Test fun oversizedExistingCacheIsNotRead() {
        val path = temporary.newFile("scan.json")
        path.writeText("x".repeat(64))
        assertTrue(runCatching { BoundedMetadataCache(path, 8).read() }.exceptionOrNull() is IllegalArgumentException)
        assertEquals(64L, path.length())
    }

    @Test fun malformedUtf8IsNotSilentlyReplaced() {
        val path = temporary.newFile("scan.json")
        path.writeBytes(byteArrayOf(0xc3.toByte(), 0x28))
        assertTrue(runCatching { BoundedMetadataCache(path, 128).read() }.isFailure)
    }

    @Test fun abandonedWriteDoesNotReplacePublishedVersion() {
        val path = temporary.newFile("scan.json")
        path.writeText("published")
        temporary.newFile("scan.json.interrupted.tmp").writeText("unfinished")
        assertEquals("published", BoundedMetadataCache(path, 128).read())
    }

    @Test fun cancellationBeforeAtomicPublicationKeepsPreviousVersion() {
        val cache = BoundedMetadataCache(File(temporary.root, "scan.json"), 128)
        cache.write("published")
        var checks = 0
        val error = runCatching {
            cache.write("superseded") { if (++checks == 2) throw java.util.concurrent.CancellationException() }
        }.exceptionOrNull()
        assertTrue(error is java.util.concurrent.CancellationException)
        assertEquals("published", cache.read())
        assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })
    }
}
