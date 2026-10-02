package dev.klbt.ageds

/**
 * Per-owner ordering between generation invalidation and final cache publication.
 * This is not a process-wide or cross-process cache lock.
 */
class SourceScanPublicationGate {
    /** Identity is the token: no numeric generation counter can wrap or be reused. */
    class Token internal constructor()

    private val lock = Any()
    private var current: Token? = null

    /** Begin a new generation and supersede any previously issued token. */
    fun begin(): Token = synchronized(lock) {
        Token().also { current = it }
    }

    /** Once this returns, an older token cannot enter a new publication action. */
    fun invalidate() = synchronized(lock) {
        current = null
    }

    /** A snapshot check for UI work, not a substitute for guarded publication. */
    fun isCurrent(token: Token): Boolean = synchronized(lock) {
        current === token
    }

    /**
     * Execute only the final atomic file move while holding the same lock used
     * by invalidate/begin. Encoding, writes, fsync and staging happen beforehand.
     *
     * False means stale and [publish] was not invoked. Exceptions propagate and
     * release the lock. A successful token remains current for subsequent UI
     * checks; this method does not enforce one publication per token.
     */
    fun publishIfCurrent(token: Token, publish: () -> Unit): Boolean = synchronized(lock) {
        if (current !== token) return@synchronized false
        publish()
        true
    }
}
