package dev.klbt.ageds

import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class AccountSessionTest {
    private class MemoryStore : KeyValueStore {
        val values = mutableMapOf<String, String?>()
        override fun get(key: String) = values[key]
        override fun put(key: String, value: String?) { values[key] = value }
    }

    private val store = MemoryStore()
    private var clears = 0
    private val session = AccountSession(store) { clears++ }

    @After fun reset() { ClientCredentials.token = null }

    @Test fun firstActivationClearsAndRemembersTheAccount() {
        assertTrue(session.activate("https://ageds.example/", "abc123.secret"))
        assertEquals(1, clears)
        assertEquals("abc123.secret", ClientCredentials.token)
        assertFalse(session.activate("https://ageds.example", "abc123.secret"))
        assertEquals(1, clears)
    }

    @Test fun changingTheTokenOrServerClearsServerDerivedState() {
        session.activate("https://ageds.example", "abc123.secret")
        assertTrue(session.activate("https://ageds.example", "def456.other"))
        assertTrue(session.activate("https://other.example", "def456.other"))
        assertTrue(session.activate("https://other.example", null))
        assertEquals(4, clears)
        assertNull(ClientCredentials.token)
        assertTrue(ClientCredentials.headers().isEmpty())
    }

    @Test fun theAccountKeyNeverContainsTheSecret() {
        val key = AccountSession.accountKey("https://ageds.example", "abc123.very-secret-part")
        assertFalse(key.contains("very-secret-part"))
        assertEquals(64, key.length)
        // Rotating only the secret of the same token method keeps the same account.
        assertEquals(key, AccountSession.accountKey("https://ageds.example/", "abc123.another-secret"))
        assertNotEquals(key, AccountSession.accountKey("https://ageds.example", "zzz999.very-secret-part"))
        session.activate("https://ageds.example", "abc123.very-secret-part")
        assertFalse(store.values.values.any { it?.contains("very-secret-part") == true })
    }

    @Test fun unauthorizedClearsAndForcesAFreshActivation() {
        session.activate("https://ageds.example", "abc123.secret")
        session.onUnauthorized()
        assertTrue(session.unauthorized)
        assertEquals(2, clears)
        assertTrue(session.activate("https://ageds.example", "abc123.secret"))
        assertFalse(session.unauthorized)
        assertEquals(3, clears)
    }

    @Test fun headersCarryTheTokenOnlyWhenPresent() {
        session.activate("https://ageds.example", "  abc123.secret  ")
        assertEquals(mapOf("X-AGEDS-Token" to "abc123.secret"), ClientCredentials.headers())
        session.activate("https://ageds.example", "   ")
        assertTrue(ClientCredentials.headers().isEmpty())
    }
}
