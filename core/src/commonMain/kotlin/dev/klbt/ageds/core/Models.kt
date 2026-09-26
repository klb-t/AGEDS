package dev.klbt.ageds.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

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
)

/**
 * Deliberately deterministic. This is a scheduling heuristic, not an epistemic judgment.
 * Human priority dominates; duration and evidence hints only break ties.
 */
data class PrioritySignals(
    val manualPriority: Int = 0,
    val durationSeconds: Long? = null,
    val hasKnownCounterparty: Boolean = false,
    val hasConflict: Boolean = false,
    val taggedLegal: Boolean = false,
)

object TranscriptionPriority {
    fun score(s: PrioritySignals): Int {
        var score = s.manualPriority.coerceIn(0, 100) * 100
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
