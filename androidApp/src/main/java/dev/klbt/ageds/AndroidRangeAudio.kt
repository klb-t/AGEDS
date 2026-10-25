package dev.klbt.ageds

import android.content.Context
import android.media.MediaPlayer
import android.net.Uri

/** One decoder at a time; release also abandons outstanding asynchronous preparation.
 * With a context, the account token travels as a request header, never in the URL. */
class AndroidRangeAudio(private val context: Context? = null) : RangeAudioBackend {
    private val player = MediaPlayer()
    override fun prepare(url: String, ready: () -> Unit, failure: (String) -> Unit) {
        player.setOnPreparedListener { ready() }
        player.setOnCompletionListener { failure("Koniec materiału") }
        player.setOnErrorListener { _, what, extra -> failure("MediaPlayer $what/$extra"); true }
        val headers = ClientCredentials.headers()
        if (context != null && headers.isNotEmpty()) player.setDataSource(context, Uri.parse(url), headers)
        else player.setDataSource(url)
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
