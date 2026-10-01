package dev.klbt.ageds.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
data class ArtifactSummary(
    val id: Long,
    val originalName: String,
    val mimeType: String? = null,
    val sizeBytes: Long? = null,
    val sha256: String? = null,
    val createdAt: String? = null,
    val sourceLocator: String? = null,
    val transcriptStatus: String = "none",
    val transcriptLanguage: String? = null,
    val transcriptPreview: String? = null,
    val queuedPriority: Int? = null,
    val tags: List<String> = emptyList(),
)

@Serializable
data class TranscriptWord(
    val start: Double? = null,
    val end: Double? = null,
    val word: String,
    val probability: Double? = null,
)

@Serializable
data class TranscriptSegment(
    val start: Double,
    val end: Double,
    val text: String,
    val words: List<TranscriptWord> = emptyList(),
)

@Serializable
data class Transcript(
    val id: Long,
    val artifactId: Long,
    val model: String? = null,
    val language: String? = null,
    val text: String,
    val segments: List<TranscriptSegment> = emptyList(),
    val confidence: Double? = null,
    val createdAt: String? = null,
    val runId: Long? = null,
    val metadata: JsonObject? = null,
    val wordTiming: WordTiming? = null,
)

@Serializable
data class Annotation(
    val id: Long,
    val artifactId: Long? = null,
    val kind: String = "note",
    val label: String? = null,
    val body: String,
    val startMs: Long? = null,
    val endMs: Long? = null,
    val createdAt: String? = null,
    val derivedTextId: Long? = null,
)

@Serializable
data class UploadResult(
    val ok: Boolean,
    @SerialName("artifact_id") val artifactId: Long,
    val sha256: String? = null,
)

@Serializable
data class QueueResult(@SerialName("job_id") val jobId: Long)

@Serializable
data class AnnotationCreate(
    val body: String,
    val label: String? = null,
    val kind: String = "note",
    val startMs: Long? = null,
    val endMs: Long? = null,
    val derivedTextId: Long? = null,
)

/**
 * Deliberately deterministic. This is a scheduling heuristic, not an epistemic judgment.
 * Human priority is lexicographically dominant; derived hints only order items
 * inside the same manual-priority bucket.
 */
data class PrioritySignals(
    val manualPriority: Int = 0,
    val durationSeconds: Long? = null,
    val hasKnownCounterparty: Boolean = false,
    val hasConflict: Boolean = false,
    val taggedLegal: Boolean = false,
)

object TranscriptionPriority {
    private const val MANUAL_STRIDE = 10_000

    fun score(s: PrioritySignals): Int {
        var score = s.manualPriority.coerceIn(0, 100) * MANUAL_STRIDE
        if (s.taggedLegal) score += 600
        if (s.hasConflict) score += 400
        if (s.hasKnownCounterparty) score += 100
        val d = s.durationSeconds ?: 0
        score += when {
            d >= 1200 -> 180
            d >= 300 -> 120
            d >= 60 -> 60
            else -> 0
        }
        return score
    }
}

@Serializable
data class TranscriptVersion(
    val id: Long,
    @SerialName("artifact_id") val artifactId: Long,
    @SerialName("run_id") val runId: Long? = null,
    val model: String? = null,
    val language: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

/** Availability describes stored ASR marks; it does not verify alignment or audio. */
@Serializable
data class WordTiming(
    @SerialName("schema_version") val schemaVersion: Int = 1,
    val kind: String = "asr_word_timing",
    val status: String = "unavailable",
    val segments: List<WordTimingSegment> = emptyList(),
    @SerialName("coverage_complete") val coverageComplete: Boolean = false,
    val reason: String? = null,
)

@Serializable
data class WordTimingSegment(
    @SerialName("segment_index") val segmentIndex: Int,
    @SerialName("word_selection_available") val wordSelectionAvailable: Boolean,
    val reason: String? = null,
    @SerialName("word_count") val wordCount: Int? = null,
)

@Serializable
data class WordRef(
    @SerialName("segment_index") val segmentIndex: Int,
    @SerialName("word_index") val wordIndex: Int,
)

@Serializable
data class CitationCreate(
    val derivedTextId: Long,
    val segmentIndices: List<Int>? = null,
    val wordRefs: List<WordRef>? = null,
    val quoteText: String? = null,
)

@Serializable
data class Citation(
    val id: Long,
    @SerialName("artifact_id") val artifactId: Long,
    @SerialName("derived_text_id") val derivedTextId: Long,
    @SerialName("start_ms") val startMs: Long,
    @SerialName("end_ms") val endMs: Long,
    @SerialName("quote_text") val quoteText: String,
    @SerialName("quote_sha256") val quoteSha256: String,
    val selector: JsonObject,
    @SerialName("created_at") val createdAt: String? = null,
    val validation: String? = null,
    @SerialName("audio_verification") val audioVerification: String? = null,
)

/** Bounded descending-ID snapshot page; item wire shapes remain endpoint-specific. */
@Serializable
data class ArtifactPage<T>(
    val artifactId: Long,
    val items: List<T>,
    val nextBeforeId: Long?,
    val snapshotMaxId: Long,
    val hasMore: Boolean,
    val limit: Int,
)
