package dev.klbt.ageds.core

/**
 * AGEDS invariant: evidence bytes and source metadata are immutable inputs.
 * Every parser/transcript/correlation/annotation is a separately versioned assertion.
 */
object EvidenceInvariant {
    const val RAW_IS_IMMUTABLE = true
    const val DERIVED_NEVER_OVERWRITES_RAW = true
    const val CORRELATION_IS_A_RELATION = true
    const val UNCERTAINTY_MUST_BE_EXPLICIT = true
}

