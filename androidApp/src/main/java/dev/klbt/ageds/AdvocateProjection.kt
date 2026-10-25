package dev.klbt.ageds

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/** Words, not numbers: a label names the kind of statement, never a probability. */
object AdvocateProjection {
    private val labels = mapOf(
        "quoted_material" to "cytat z zapisanego tekstu",
        "proven" to "wynika z cytowanych fragmentów",
        "claim" to "twierdzenie zawarte w materiale",
        "hypothesis" to "wniosek (hipoteza)",
        "unknown" to "brak podstawy w materiale",
        "claim_without_basis" to "bez cytatu – niezweryfikowane",
    )
    private val kinds = mapOf(
        "transcript_segment" to "transkrypcja nagrania", "text_window" to "fragment tekstu",
        "event" to "wiadomość", "assignment" to "interpretacja w sprawie", "fragment_quote" to "udostępniony fragment",
    )

    fun label(status: String): String = labels[status] ?: status
    fun kind(kind: String): String = kinds[kind] ?: kind

    fun mode(answer: AdvocateAnswer, providerModel: String?): String = when {
        answer.unavailable -> "Ta odpowiedź opierała się na materiale, który nie jest już dla Ciebie dostępny."
        answer.mode == "retrieval_only" -> "Wyszukane fragmenty materiału (bez modelu językowego)."
        else -> "Odpowiedź modelu ${providerModel ?: answer.mode.removePrefix("provider:")} (Google Vertex) — sprawdź cytaty przed użyciem."
    }

    fun fallback(answer: AdvocateAnswer): String? =
        (answer.verification?.get("provider_fallback") as? JsonPrimitive)?.contentOrNull?.let { "Model nie został użyty: $it" }

    fun ownerVisibility(value: String): String = when (value) {
        "none" -> "Właściciel materiału nie widzi treści tej rozmowy."
        "full" -> "Właściciel materiału może czytać tę rozmowę."
        "metadata_only" -> "Właściciel materiału widzi, że rozmowa istnieje, ale nie jej treść."
        "opt_in_by_recipient" -> "Właściciel materiału nie widzi rozmowy, dopóki jej nie udostępnisz."
        else -> "Widoczność rozmowy dla właściciela: $value."
    }

    /** Citations resolve only into the answer's own basis; anything else shows as unavailable. */
    fun resolve(ref: String, basis: List<AdvocateBasisEntry>): AdvocateBasisEntry =
        basis.firstOrNull { it.ref == ref } ?: AdvocateBasisEntry(ref = ref, unavailable = true)

    fun clock(ms: Long): String {
        val seconds = ms / 1000
        return "%02d:%02d".format(seconds / 60, seconds % 60)
    }

    fun heading(entry: AdvocateBasisEntry): String = buildList {
        add(entry.ref)
        add(kind(entry.kind))
        entry.artifact?.name?.takeIf { it.isNotEmpty() }?.let { add(it) }
        if (entry.startMs != null && entry.endMs != null) add("${clock(entry.startMs)}–${clock(entry.endMs)}")
    }.joinToString(" · ")
}
