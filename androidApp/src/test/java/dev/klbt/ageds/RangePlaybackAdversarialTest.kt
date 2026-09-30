package dev.klbt.ageds

import org.junit.Assert.*
import org.junit.Test

class RangePlaybackAdversarialTest {
    private class FakeAudio : RangeAudioBackend {
        lateinit var prepared: () -> Unit
        lateinit var failed: (String) -> Unit
        lateinit var sought: () -> Unit
        var url = ""
        var seek: Long? = null
        var position = 0L
        var starts = 0
        var releases = 0
        var throwStart = false
        var duration = 10_000L
        var seekOffset = 0L
        override fun prepare(url: String, ready: () -> Unit, failure: (String) -> Unit) { this.url=url; prepared=ready; failed=failure }
        override fun seek(positionMs: Long, ready: () -> Unit) { seek=positionMs; position=positionMs+seekOffset; sought=ready }
        override fun start() { if (throwStart) error("decoder failed"); starts++ }
        override fun positionMs() = position
        override fun durationMs() = duration
        override fun release() { releases++ }
    }
    private class Fixture {
        val backends = mutableListOf<FakeAudio>()
        val player = RangePlaybackController { FakeAudio().also { backends.add(it) } }
        fun play(url: String="server-a/artifact-7",start:Long=100,end:Long=200): FakeAudio {
            player.play(url,start,end)
            return backends.last()
        }
    }

    @Test fun playbackWaitsForSeekAndStopsAtExactEndBoundary() {
        val f=Fixture(); val a=f.play()
        assertEquals(0,a.starts)
        a.prepared(); assertEquals(100L,a.seek); assertEquals(0,a.starts)
        a.sought(); assertEquals(1,a.starts)
        a.position=199; f.player.tick(); assertEquals(0,a.releases)
        a.position=200; f.player.tick(); assertEquals(1,a.releases)
        f.player.tick(); assertEquals(1,a.releases)
    }
    @Test fun replacedServerArtifactPlaybackIgnoresLatePrepareAndFailure() {
        val f=Fixture(); val old=f.play(); val active=f.play("server-b/artifact-9",400,800)
        assertEquals(1,old.releases)
        old.prepared(); old.failed("stale error")
        assertNull(old.seek); assertEquals(0,old.starts); assertEquals(0,active.releases)
        active.prepared(); active.sought(); assertEquals(1,active.starts)
    }
    @Test fun changedVersionRangeIgnoresLateSeekCallback() {
        val f=Fixture(); val old=f.play(); old.prepared()
        val active=f.play(start=600,end=900)
        old.sought(); assertEquals(0,old.starts)
        active.prepared(); active.sought(); assertEquals(600L,active.seek); assertEquals(1,active.starts)
    }
    @Test fun disposalDuringPrepareRejectsEveryLateCallback() {
        val f=Fixture(); val a=f.play(); f.player.stop()
        a.prepared(); a.failed("late"); f.player.tick()
        assertEquals(1,a.releases); assertEquals(0,a.starts); assertNull(a.seek)
        assertEquals("Zatrzymane",f.player.status)
    }
    @Test fun pauseDuringPendingSeekAndRepeatedStopReleaseOnlyOnce() {
        val f=Fixture(); val a=f.play(); a.prepared(); f.player.stop(); f.player.stop(); a.sought()
        assertEquals(1,a.releases); assertEquals(0,a.starts)
    }
    @Test fun decoderStartFailureReleasesAndDoesNotLeaveActivePlayback() {
        val f=Fixture(); val a=f.play(); a.throwStart=true; a.prepared(); a.sought()
        assertEquals(1,a.releases); assertTrue(f.player.status.contains("decoder failed"))
        a.failed("late failure"); assertTrue(f.player.status.contains("decoder failed"))
    }
    @Test fun invalidRangesDoNotCreateDecoderAndStopExistingAudio() {
        for ((start,end) in listOf(-1L to 1L,100L to 100L,200L to 100L,0L to (Int.MAX_VALUE.toLong()+1))) {
            val f=Fixture(); val a=f.play()
            try { f.player.play("bad",start,end); fail("range $start $end accepted") } catch (_:IllegalArgumentException) {}
            assertEquals(1,f.backends.size); assertEquals(1,a.releases)
        }
    }
    @Test fun requestEpochDoesNotReviveEarlierCallbacksWhenContextReturns() {
        val epoch=RequestEpoch(); val a=epoch.current(); val b=epoch.invalidate(); val aAgain=epoch.invalidate()
        assertFalse(epoch.accepts(a)); assertFalse(epoch.accepts(b)); assertTrue(epoch.accepts(aAgain))
    }
    @Test fun hungPreparationTimesOutAndRejectsLateReady() {
        var now=0L; val audio=FakeAudio()
        val player=RangePlaybackController({ now }) { audio }
        player.play("fixture",100,200)
        now=30_000_000_000L; player.tick()
        assertEquals(1,audio.releases); audio.prepared(); assertNull(audio.seek)
    }
    @Test fun stalledPlaybackHasFiniteResourceLifetime() {
        var now=0L; val audio=FakeAudio()
        val player=RangePlaybackController({ now }) { audio }
        player.play("fixture",100,200); audio.prepared(); audio.sought()
        now=30_100_000_000L; player.tick()
        assertEquals(1,audio.releases); assertTrue(player.status.startsWith("Audio:"))
    }

    @Test fun rangeOutsideActualMediaDurationNeverSeeksOrStarts() {
        for (duration in listOf(0L,99L,150L)) {
            val f=Fixture(); val a=f.play(); a.duration=duration; a.prepared()
            assertNull(a.seek); assertEquals(0,a.starts); assertEquals(1,a.releases)
        }
    }
    @Test fun seekLandingOutsideSelectedRangeNeverStarts() {
        for (offset in listOf(-1L,100L)) {
            val f=Fixture(); val a=f.play(); a.seekOffset=offset; a.prepared(); a.sought()
            assertEquals(0,a.starts); assertEquals(1,a.releases)
        }
    }
    @Test fun rangeEndingExactlyAtMediaDurationIsPlayable() {
        val f=Fixture(); val a=f.play(); a.duration=200; a.prepared(); a.sought()
        assertEquals(1,a.starts); a.position=200; f.player.tick(); assertEquals(1,a.releases)
    }

}
