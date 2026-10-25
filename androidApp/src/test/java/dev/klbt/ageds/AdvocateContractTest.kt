package dev.klbt.ageds

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

/** Fixtures are real server responses generated from synthetic data (server/tests style). */
class AdvocateContractTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private fun read(name: String): String =
        requireNotNull(javaClass.getResource("/advocate/$name.json")) { "missing fixture $name" }.readText()

    @Test fun statusWithoutProviderIsRetrievalOnly() {
        val status = json.decodeFromString<AdvocateStatus>(read("status"))
        assertTrue(status.canChat)
        assertNull(status.provider)
        assertEquals("tokens", status.authMode)
        assertEquals("Właściciel materiału nie widzi treści tej rozmowy.", AdvocateProjection.ownerVisibility(status.ownerVisibility))
    }

    @Test fun answerPinsItsBasisAndEveryCitationResolves() {
        val answer = json.decodeFromString<AdvocateAnswer>(read("answer"))
        assertEquals("retrieval_only", answer.mode)
        val basis = requireNotNull(answer.basis)
        for (claim in answer.claims) for (ref in claim.citations) assertFalse(AdvocateProjection.resolve(ref, basis).unavailable)
        val segment = basis.first { it.kind == "transcript_segment" }
        assertEquals(64, segment.artifact?.sha256?.length)
        assertNotNull(segment.derivedTextId)
        assertEquals("p1 · transkrypcja nagrania · rozmowa.wav · 00:00–00:02", AdvocateProjection.heading(segment))
        assertEquals("cytat z zapisanego tekstu", AdvocateProjection.label(answer.claims.first().status))
        assertEquals("Wyszukane fragmenty materiału (bez modelu językowego).", AdvocateProjection.mode(answer, null))
    }

    @Test fun historyAndListsDecode() {
        val history = json.decodeFromString<AdvocateHistory>(read("history"))
        assertEquals(1, history.turns.size)
        assertFalse(history.turns.single().answers.single().unavailable)
        val conversations = json.decodeFromString<List<AdvocateConversation>>(read("conversations"))
        assertEquals(json.decodeFromString<AdvocateConversation>(read("conversation")).uid, conversations.single().uid)
        val basis = json.decodeFromString<AdvocateBasis>(read("basis"))
        assertTrue(basis.entries.none { it.unavailable })
    }

    @Test fun revokedMaterialIsWithheldNotShown() {
        val answer = json.decodeFromString<AdvocateHistory>(read("history_revoked")).turns.single().answers.single()
        assertTrue(answer.unavailable)
        assertTrue(answer.claims.isEmpty())
        assertTrue(AdvocateProjection.mode(answer, null).contains("nie jest już dla Ciebie dostępny"))
        val basis = json.decodeFromString<AdvocateBasis>(read("basis_revoked"))
        assertTrue(basis.entries.all { it.unavailable && it.text == null })
    }

    @Test fun unknownStatusAndKindPassThroughUnchanged() {
        assertEquals("pewne_na_100", AdvocateProjection.label("pewne_na_100"))
        assertEquals("nowy_rodzaj", AdvocateProjection.kind("nowy_rodzaj"))
        assertTrue(AdvocateProjection.resolve("p9", emptyList()).unavailable)
        assertEquals("Widoczność rozmowy dla właściciela: custom:x.", AdvocateProjection.ownerVisibility("custom:x"))
    }

    @Test fun modelModeAndFallbackAreNamed() {
        val raw = """{"answer_uid":"a","mode":"provider:vertex","claims":[],"verification":{"provider_fallback":"daily budget 1.0 EUR would be exceeded"}}"""
        val answer = json.decodeFromString<AdvocateAnswer>(raw)
        assertEquals("Odpowiedź modelu gemini-3.5-flash (Google Vertex) — sprawdź cytaty przed użyciem.", AdvocateProjection.mode(answer, "gemini-3.5-flash"))
        assertEquals("Model nie został użyty: daily budget 1.0 EUR would be exceeded", AdvocateProjection.fallback(answer))
        assertEquals("01:05", AdvocateProjection.clock(65_400))
    }
}
