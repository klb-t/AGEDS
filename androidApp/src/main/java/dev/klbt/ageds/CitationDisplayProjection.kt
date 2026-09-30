package dev.klbt.ageds

import dev.klbt.ageds.core.Transcript
import dev.klbt.ageds.core.WordRef

data class CitationWordDisplay(val words: List<Pair<WordRef, String>>, val truncated: Boolean)

/** Only materialize the visible prefix plus one sentinel, retaining exact original indices. */
object CitationDisplayProjection {
    const val WORD_LIMIT = 10_000
    fun words(transcript: Transcript?): CitationWordDisplay {
        val prefix = transcript?.segments?.asSequence()?.flatMapIndexed { si, segment ->
            segment.words.asSequence().mapIndexed { wi, word -> WordRef(si, wi) to word.word }
        }?.take(WORD_LIMIT + 1)?.toList() ?: emptyList()
        return CitationWordDisplay(prefix.take(WORD_LIMIT), prefix.size > WORD_LIMIT)
    }
}
