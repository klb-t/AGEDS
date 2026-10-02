package dev.klbt.ageds

import android.media.MediaPlayer

/** One decoder at a time; release also abandons outstanding asynchronous preparation. */
class AndroidRangeAudio : RangeAudioBackend {
    private val player = MediaPlayer()
    override fun prepare(url: String, ready: () -> Unit, failure: (String) -> Unit) {
        player.setOnPreparedListener { ready() }
        player.setOnCompletionListener { failure("Koniec materiału") }
        player.setOnErrorListener { _, what, extra -> failure("MediaPlayer $what/$extra"); true }
        player.setDataSource(url)
        player.prepareAsync()
    }
    override fun seek(positionMs: Long, ready: () -> Unit) {
        player.setOnSeekCompleteListener { ready() }
        player.seekTo(positionMs, MediaPlayer.SEEK_CLOSEST)
    }
    override fun start() = player.start()
    override fun durationMs(): Long = player.duration.toLong()
    override fun positionMs(): Long = player.currentPosition.toLong()
    override fun release() {
        player.setOnPreparedListener(null)
        player.setOnSeekCompleteListener(null)
        player.setOnErrorListener(null)
        player.setOnCompletionListener(null)
        player.release()
    }
}
