package dev.klbt.ageds

import java.security.MessageDigest

/** The token the HTTP clients send; null means the server's legacy_open mode. */
object ClientCredentials {
    @Volatile var token: String? = null
    const val HEADER = "X-AGEDS-Token"
    fun headers(): Map<String, String> = token?.let { mapOf(HEADER to it) } ?: emptyMap()
}

interface KeyValueStore {
    fun get(key: String): String?
    fun put(key: String, value: String?)
}

/**
 * Server-derived state belongs to one account (server + token identity). Changing
 * either, or a 401 from the server, clears that state before anything else is shown.
 * The account key hashes the server and the token's public method id, never the secret.
 */
class AccountSession(private val store: KeyValueStore, private val clearScoped: () -> Unit) {
    var unauthorized: Boolean = false
        private set

    fun activate(serverUrl: String, token: String?): Boolean {
        val key = accountKey(serverUrl, token)
        val changed = store.get(KEY) != key
        if (changed) {
            clearScoped()
            store.put(KEY, key)
        }
        ClientCredentials.token = token?.trim()?.takeIf { it.isNotEmpty() }
        unauthorized = false
        return changed
    }

    /** The token was refused (revoked, expired, wrong): drop everything read with it. */
    fun onUnauthorized() {
        clearScoped()
        store.put(KEY, null)
        unauthorized = true
    }

    companion object {
        const val KEY = "scopedCacheAccount"

        fun accountKey(serverUrl: String, token: String?): String {
            val methodId = token?.trim()?.substringBefore('.')?.takeIf { it.isNotEmpty() } ?: "legacy_open"
            val digest = MessageDigest.getInstance("SHA-256")
                .digest("${serverUrl.trim().trimEnd('/')}|$methodId".toByteArray(Charsets.UTF_8))
            return digest.joinToString("") { "%02x".format(it) }
        }
    }
}
