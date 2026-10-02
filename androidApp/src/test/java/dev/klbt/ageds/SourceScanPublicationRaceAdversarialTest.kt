package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** Deterministic synthetic metadata races; no scheduling sleeps or Android provider. */
class SourceScanPublicationRaceAdversarialTest {
    @get:Rule val temporary = TemporaryFolder()
    private fun await(latch: CountDownLatch) = check(latch.await(5, TimeUnit.SECONDS)) { "barrier timeout" }
    private fun join(thread: Thread) {
        thread.join(5000)
        check(!thread.isAlive) { "thread failed to terminate" }
    }
    private fun worker(error: AtomicReference<Throwable?>, action: () -> Unit): Thread = Thread {
        try { action() } catch (failure: Throwable) { error.set(failure) }
    }.apply { isDaemon = true; start() }
    private fun assertClean() = assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })

    @Test fun oldPreMoveCancellationCheckAllowsPublicationAfterInvalidation() {
        val file = File(temporary.root, "scan.json")
        val cache = BoundedMetadataCache(file, 1024)
        cache.write("{\"version\":\"prior\"}")
        val checked = CountDownLatch(1)
        val release = CountDownLatch(1)
        val cancelled = AtomicBoolean(false)
        val error = AtomicReference<Throwable?>()
        val thread = worker(error) {
            var checks = 0
            cache.write("{\"version\":\"obsolete\"}") {
                check(!cancelled.get())
                if (++checks == 2) { checked.countDown(); await(release) }
            }
        }
        try {
            await(checked)
            cancelled.set(true)
        } finally { release.countDown(); join(thread) }
        assertNull(error.get())
        assertTrue(cancelled.get())
        assertEquals("{\"version\":\"obsolete\"}", cache.read())
        assertClean()
    }

    @Test fun invalidationBeforeGuardPreservesPriorBytesAndRemovesStaging() {
        val file = File(temporary.root, "scan.json")
        val cache = BoundedMetadataCache(file, 1024)
        cache.write("{\"literal\":\"ą prior\"}")
        val prior = file.readBytes()
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val staged = CountDownLatch(1)
        val release = CountDownLatch(1)
        val error = AtomicReference<Throwable?>()
        val published = AtomicReference<Boolean?>()
        val thread = worker(error) {
            var checks = 0
            published.set(cache.writeGuarded("obsolete", gate, token) {
                if (++checks == 2) { staged.countDown(); await(release) }
            })
        }
        try { await(staged); gate.invalidate() }
        finally { release.countDown(); join(thread) }
        assertNull(error.get())
        assertEquals(false, published.get())
        assertArrayEquals(prior, file.readBytes())
        assertClean()
    }

    @Test fun publicationHoldingGuardMakesInvalidationWaitForRealAtomicMove() {
        val file = File(temporary.root, "scan.json")
        file.writeText("prior")
        val inMove = CountDownLatch(1)
        val release = CountDownLatch(1)
        val cache = BoundedMetadataCache(file, 1024) { source, target ->
            inMove.countDown()
            await(release)
            Files.move(source, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            Unit
        }
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val error = AtomicReference<Throwable?>()
        val cancellationError = AtomicReference<Throwable?>()
        val invalidated = AtomicBoolean(false)
        val published = AtomicReference<Boolean?>()
        val writer = worker(error) { published.set(cache.writeGuarded("legitimate-new", gate, token)) }
        var canceller: Thread? = null
        try {
            await(inMove)
            canceller = worker(cancellationError) { gate.invalidate(); invalidated.set(true) }
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
            while (canceller.state != Thread.State.BLOCKED && System.nanoTime() < deadline) Thread.yield()
            assertEquals(Thread.State.BLOCKED, canceller.state)
            assertFalse(invalidated.get())
            assertEquals("prior", file.readText())
        } finally { release.countDown(); join(writer); canceller?.let(::join) }
        assertNull(error.get())
        assertNull(cancellationError.get())
        assertEquals(true, published.get())
        assertTrue(invalidated.get())
        assertFalse(gate.isCurrent(token))
        assertEquals("legitimate-new", cache.read())
        assertClean()
    }

    @Test fun supersedingTokenPublishesNewerWhileOldStagedWriterCannotOverwrite() {
        val cache = BoundedMetadataCache(File(temporary.root, "scan.json"), 1024)
        cache.write("prior")
        val gate = SourceScanPublicationGate()
        val old = gate.begin()
        val staged = CountDownLatch(1)
        val release = CountDownLatch(1)
        val error = AtomicReference<Throwable?>()
        val oldPublished = AtomicReference<Boolean?>()
        val writer = worker(error) {
            var checks = 0
            oldPublished.set(cache.writeGuarded("obsolete", gate, old) {
                if (++checks == 2) { staged.countDown(); await(release) }
            })
        }
        try {
            await(staged)
            val current = gate.begin()
            assertTrue(cache.writeGuarded("newer", gate, current))
            assertEquals("newer", cache.read())
        } finally { release.countDown(); join(writer) }
        assertNull(error.get())
        assertEquals(false, oldPublished.get())
        assertEquals("newer", cache.read())
        assertClean()
    }
}
