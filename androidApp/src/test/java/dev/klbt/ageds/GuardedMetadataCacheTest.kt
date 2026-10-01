package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException
import java.util.concurrent.CancellationException

class GuardedMetadataCacheTest {
    @get:Rule val temporary = TemporaryFolder()
    private fun cacheFile() = File(temporary.root, "scan.json")
    private fun assertOnlyCacheRemains() = assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })

    @Test fun currentTokenPublishesAndLaterInvalidationDoesNotRollBack() {
        val cache = BoundedMetadataCache(cacheFile(), 128)
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        assertTrue(cache.writeGuarded("źródło", gate, token))
        gate.invalidate()
        assertEquals("źródło", cache.read())
        assertOnlyCacheRemains()
    }

    @Test fun supersessionAfterStagingSkipsActualReplacement() {
        val file = cacheFile().apply { writeText("prior") }
        var replacements = 0
        val cache = BoundedMetadataCache(file, 128) { _, _ -> replacements++ }
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        var checkpoints = 0
        val published = cache.writeGuarded("superseded", gate, token) {
            if (++checkpoints == 2) gate.begin()
        }
        assertFalse(published)
        assertEquals(0, replacements)
        assertEquals("prior", cache.read())
        assertOnlyCacheRemains()
    }

    @Test fun cancellationAfterStagingCleansTemporaryAndPreservesPriorFile() {
        val file = cacheFile().apply { writeText("prior") }
        val cache = BoundedMetadataCache(file, 128)
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        var checkpoints = 0
        val failure = runCatching {
            cache.writeGuarded("canceled", gate, token) {
                if (++checkpoints == 2) throw CancellationException("test canceled")
            }
        }.exceptionOrNull()
        assertTrue(failure is CancellationException)
        assertEquals("prior", cache.read())
        assertOnlyCacheRemains()
    }

    @Test fun atomicReplacementFailurePropagatesWithoutRemovingPriorSnapshot() {
        val file = cacheFile().apply { writeText("prior") }
        val cache = BoundedMetadataCache(file, 128) { _, _ -> throw IOException("atomic replacement unavailable") }
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val failure = runCatching { cache.writeGuarded("failed", gate, token) }.exceptionOrNull()
        assertTrue(failure is IOException)
        assertTrue(gate.isCurrent(token))
        assertEquals("prior", cache.read())
        assertOnlyCacheRemains()
    }

    @Test fun utf8BudgetRejectsBeforeReplacementAndLeavesPriorCache() {
        val file = cacheFile().apply { writeText("old") }
        var replacements = 0
        val cache = BoundedMetadataCache(file, 8) { _, _ -> replacements++ }
        val gate = SourceScanPublicationGate()
        val failure = runCatching { cache.writeGuarded("ą".repeat(5), gate, gate.begin()) }.exceptionOrNull()
        assertTrue(failure is IllegalArgumentException)
        assertEquals(0, replacements)
        assertEquals("old", cache.read())
        assertOnlyCacheRemains()
    }

    @Test fun legacyTrailingLambdaStillReceivesBothCheckpointsAndPublishes() {
        val cache = BoundedMetadataCache(cacheFile(), 128)
        var checkpoints = 0
        cache.write("legacy raw") { checkpoints++ }
        assertEquals(2, checkpoints)
        assertEquals("legacy raw", cache.read())
        assertOnlyCacheRemains()
    }
}
