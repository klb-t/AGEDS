package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class SourceScanPublicationGateTest {
    @Test fun beginSupersedesOldIdentityAndInvalidateRejectsPublication() {
        val gate = SourceScanPublicationGate()
        val first = gate.begin()
        assertTrue(gate.isCurrent(first))
        val second = gate.begin()
        assertNotSame(first, second)
        assertFalse(gate.isCurrent(first))
        var moves = 0
        assertFalse(gate.publishIfCurrent(first) { moves++ })
        assertTrue(gate.publishIfCurrent(second) { moves++ })
        assertTrue(gate.isCurrent(second))
        gate.invalidate()
        assertFalse(gate.publishIfCurrent(second) { moves++ })
        assertEquals(1, moves)
    }

    @Test fun tokensFromOtherOwnersNeverAuthorizePublication() {
        val gate = SourceScanPublicationGate()
        val other = SourceScanPublicationGate()
        val foreign = other.begin()
        val own = gate.begin()
        assertFalse(gate.isCurrent(foreign))
        assertFalse(gate.publishIfCurrent(foreign) { fail("Foreign token cannot publish") })
        assertTrue(gate.publishIfCurrent(own) {})
        assertTrue(other.isCurrent(foreign))
    }

    @Test fun exceptionsPropagateAndReleaseLockForANewGeneration() {
        val gate = SourceScanPublicationGate()
        val failed = gate.begin()
        try {
            gate.publishIfCurrent(failed) { throw IOException("synthetic move failure") }
            fail("Move failure must escape")
        } catch (expected: IOException) {
            assertEquals("synthetic move failure", expected.message)
        }
        assertTrue(gate.isCurrent(failed))
        gate.invalidate()
        val fresh = gate.begin()
        assertTrue(gate.publishIfCurrent(fresh) {})
        assertFalse(gate.isCurrent(failed))
    }

    @Test fun publicationHoldingLockWinsBeforeInvalidationCompletes() {
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val invalidationStarted = CountDownLatch(1)
        val invalidated = CountDownLatch(1)
        val published = AtomicBoolean(false)
        val publisher = thread(name = "synthetic-publisher") {
            published.set(gate.publishIfCurrent(token) {
                entered.countDown()
                check(release.await(5, TimeUnit.SECONDS))
            })
        }
        assertTrue(entered.await(5, TimeUnit.SECONDS))
        val canceller = thread(name = "synthetic-invalidator") {
            invalidationStarted.countDown()
            gate.invalidate()
            invalidated.countDown()
        }
        try {
            assertTrue(invalidationStarted.await(5, TimeUnit.SECONDS))
            assertFalse(invalidated.await(50, TimeUnit.MILLISECONDS))
        } finally {
            release.countDown()
            publisher.join(5000)
            canceller.join(5000)
        }
        assertFalse(publisher.isAlive)
        assertFalse(canceller.isAlive)
        assertTrue(published.get())
        assertEquals(0L, invalidated.count)
        assertFalse(gate.isCurrent(token))
        assertFalse(gate.publishIfCurrent(token) { fail("Invalidated old generation cannot publish again") })
    }

    @Test fun invalidationCompletedFirstPreventsAnyPublicationAction() {
        val gate = SourceScanPublicationGate()
        val token = gate.begin()
        val invalidator = thread { gate.invalidate() }
        invalidator.join(5000)
        assertFalse(invalidator.isAlive)
        assertFalse(gate.publishIfCurrent(token) { fail("No move after invalidation won") })
        val fresh = gate.begin()
        assertNotSame(token, fresh)
        assertTrue(gate.publishIfCurrent(fresh) {})
    }
}
