package dev.klbt.ageds

/** Fence callbacks even when a transport/decoder ignores cancellation. Main-thread confined. */
class RequestEpoch {
    private var generation = 0L
    fun invalidate(): Long = ++generation
    fun current(): Long = generation
    fun accepts(token: Long): Boolean = generation == token
}

interface RangeAudioBackend {
    fun prepare(url: String, ready: () -> Unit, failure: (String) -> Unit)
    fun seek(positionMs: Long, ready: () -> Unit)
    fun start()
    fun durationMs(): Long
    fun positionMs(): Long
    fun release()
}

/** Playback uses approximate platform seek, never claims acoustic alignment verification. */
class RangePlaybackController(
    private val nanoTime: () -> Long = System::nanoTime,
    private val factory: () -> RangeAudioBackend,
) {
    private val epoch = RequestEpoch()
    private var audio: RangeAudioBackend? = null
    private var endMs = 0L
    private var deadline = 0L
    var status: String = "Zatrzymane"
        private set

    fun play(url: String, start: Long, end: Long) {
        stop()
        require(start >= 0 && end > start && end <= Int.MAX_VALUE) { "Nieprawidłowy zakres audio" }
        val token = epoch.current()
        val backend = factory()
        audio = backend
        endMs = end
        deadline = nanoTime() + 30_000_000_000L
        status = "Przygotowanie przybliżonego odsłuchu…"
        try {
            backend.prepare(url, {
                if (epoch.accepts(token)) {
                    try {
                        val duration = backend.durationMs()
                        require(duration > 0 && duration <= Int.MAX_VALUE) { "Długość audio jest niedostępna lub niewspierana" }
                        require(start < duration && end <= duration) { "Zapisany zakres wykracza poza długość audio ($duration ms)" }
                        backend.seek(start) {
                            if (epoch.accepts(token)) {
                                try {
                                    // Approximate seek may land elsewhere; never start outside the saved range.
                                    val position = backend.positionMs()
                                    require(position >= start && position < end) {
                                        "Przybliżone przewinięcie trafiło poza zapisany zakres ($position ms); odsłuch niedostępny"
                                    }
                                    backend.start(); status = "Odtwarzanie przybliżonego zakresu"
                                    deadline = nanoTime() + (end - start + 30_000) * 1_000_000
                                }
                                catch (t: Exception) { fail(token, t.message ?: "Błąd audio") }
                            }
                        }
                    } catch (t: Exception) { fail(token, t.message ?: "Błąd przewijania") }
                }
            }, { fail(token, it) })
        } catch (t: Exception) { fail(token, t.message ?: "Błąd audio") }
    }

    private fun fail(token: Long, message: String) {
        if (!epoch.accepts(token)) return
        stop(); status = "Audio: $message"
    }

    fun tick() {
        val backend = audio ?: return
        if (nanoTime() >= deadline) { fail(epoch.current(), "Przekroczony limit oczekiwania / odtwarzania"); return }
        if (status != "Odtwarzanie przybliżonego zakresu") return
        try { if (backend.positionMs() >= endMs) stop() }
        catch (t: Exception) { fail(epoch.current(), t.message ?: "Błąd audio") }
    }

    fun stop() {
        epoch.invalidate()
        val previous = audio
        audio = null
        runCatching { previous?.release() }
        status = "Zatrzymane"
    }
}
