package dev.klbt.ageds

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** A failed actual publication boundary must release the gate and clean staging. */
class SourceScanPublicationFailureTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test fun moveFailurePreservesPriorCacheCleansTemporaryAndAllowsAnotherThreadToPublish() {
        val file = File(temporary.root, "scan.json")
        file.writeText("prior")
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val failure = IOException("synthetic pre-move failure")
        val failing = BoundedMetadataCache(file, 128) { _, _ -> throw failure }
        try {
            failing.writeGuarded("unpublished", gate, token)
            fail("move failure must propagate")
        } catch (actual: IOException) {
            assertSame(failure, actual)
        }
        assertEquals("prior", file.readText())
        assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })
        assertTrue(gate.isCurrent(token))

        // A different thread establishes that an exception released the monitor;
        // re-entry on this thread alone would not detect a leaked reentrant lock.
        val error = java.util.concurrent.atomic.AtomicReference<Throwable?>()
        val completed = java.util.concurrent.CountDownLatch(1)
        val worker = Thread {
            try {
                val next = gate.begin()
                assertFalse(gate.isCurrent(token))
                val working = BoundedMetadataCache(file, 128) { source, destination ->
                    Files.move(source, destination, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
                    Unit
                }
                assertTrue(working.writeGuarded("replacement", gate, next))
            } catch (problem: Throwable) { error.set(problem) }
            finally { completed.countDown() }
        }.apply { isDaemon = true; start() }
        assertTrue("gate remained locked after failure", completed.await(5, java.util.concurrent.TimeUnit.SECONDS))
        worker.join(1000)
        error.get()?.let { throw AssertionError("subsequent publication failed", it) }
        assertEquals("replacement", file.readText())
        assertEquals(listOf("scan.json"), temporary.root.listFiles()!!.map { it.name })
    }
}
