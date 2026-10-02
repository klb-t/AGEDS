package dev.klbt.ageds.core

import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json

const val ARTIFACT_HISTORY_PAYLOAD_BYTES: Long = 4L * 1024 * 1024
private val historyRowJson = Json { encodeDefaults = true }

/** Encoded retained-model JSON bytes, including defaults/nulls; not a heap estimate. */
fun <T> serializedHistoryRowBytes(serializer: KSerializer<T>, value: T): Long =
    historyRowJson.encodeToString(serializer, value).encodeToByteArray().size.toLong()

/**
 * Immutable, artifact-scoped cursor chain. Every incoming page is validated before
 * any item is admitted. Server byte budgets may produce pages shorter than limit.
 * At the client cap, excess rows are omitted explicitly and no next fetch is allowed.
 * Server/origin scope is owned by the calling controller, not by numeric IDs.
 */
class ArtifactPageChain<T> private constructor(
    val artifactId: Long,
    val limit: Int,
    val maxItems: Int,
    val maxPayloadBytes: Long,
    private val rowSizeOf: ((T) -> Long)?,
    private val idOf: (T) -> Long,
    private val artifactIdOf: (T) -> Long?,
    private val storedItems: List<T>,
    val snapshotMaxId: Long?,
    val nextBeforeId: Long?,
    val hasMore: Boolean,
    val isLoaded: Boolean,
    val reachedClientLimit: Boolean,
    val retainedPayloadBytes: Long,
    val reachedPayloadLimit: Boolean,
) {
    constructor(
        artifactId: Long,
        limit: Int = 100,
        maxItems: Int = 1000,
        idOf: (T) -> Long,
        artifactIdOf: (T) -> Long?,
        maxPayloadBytes: Long = ARTIFACT_HISTORY_PAYLOAD_BYTES,
        rowSizeOf: ((T) -> Long)? = null,
    ) : this(artifactId, limit, maxItems, maxPayloadBytes, rowSizeOf, idOf, artifactIdOf,
        emptyList(), null, null, false, false, false, 0, false)

    init {
        require(artifactId > 0) { "Artifact ID must be positive" }
        require(limit in 1..100) { "Page limit must be between 1 and 100" }
        require(maxPayloadBytes in 1..ARTIFACT_HISTORY_PAYLOAD_BYTES) { "Payload budget must be between 1 byte and 4 MiB" }
        require(maxItems in 1..1000) { "Client maximum must be between 1 and 1000" }
    }

    val items: List<T> get() = storedItems.toList()
    // No generic toString fallback: old generic callers explicitly remain count-only.
    // Every production collection supplies a serializer-based rowSizeOf.
    val payloadBudgetEnabled: Boolean get() = rowSizeOf != null
    val canLoadMore: Boolean get() = !reachedClientLimit && !reachedPayloadLimit && (!isLoaded || hasMore)

    fun append(page: ArtifactPage<T>): ArtifactPageChain<T> {
        require(canLoadMore) { "Page chain is complete or at its client limit" }
        require(page.artifactId == artifactId) { "Page belongs to another artifact" }
        require(page.limit == limit) { "Page limit differs from the request" }
        require(page.items.size <= limit) { "Page exceeds requested limit" }
        require(page.snapshotMaxId >= 0) { "Negative snapshot maximum" }
        if (isLoaded) {
            require(page.snapshotMaxId == snapshotMaxId) { "Page snapshot changed" }
        } else if (page.items.isEmpty()) {
            require(page.snapshotMaxId == 0L) { "Empty initial page must have an empty snapshot" }
        }
        var previousId = nextBeforeId
        page.items.forEach { item ->
            val id = idOf(item)
            require(artifactIdOf(item) == artifactId) { "Page item belongs to another or unknown artifact" }
            require(id > 0 && id <= page.snapshotMaxId) { "Page item ID outside snapshot" }
            require(previousId == null || id < previousId!!) { "Page IDs must be unique and strictly descending below the cursor" }
            previousId = id
        }
        if (page.hasMore) {
            require(page.items.isNotEmpty()) { "A continuing page must contain an item" }
            require(page.nextBeforeId == idOf(page.items.last())) { "Continuation cursor must equal the final item ID" }
            require(requireNotNull(page.nextBeforeId) > 1) { "Continuation cursor cannot have an earlier positive ID" }
        } else {
            require(page.nextBeforeId == null) { "A final page must not provide a cursor" }
        }
        // Evaluate every row's cost before admission, even beyond a cutoff. A
        // malformed row must not hide behind an earlier oversized valid row.
        val costs = page.items.map { row ->
            (rowSizeOf?.invoke(row) ?: 0L).also { require(rowSizeOf == null || it > 0) { "Measured row byte cost must be positive" } }
        }
        val remaining = maxItems - storedItems.size
        var bytes = retainedPayloadBytes
        var admittedCount = 0
        var byteOmission = false
        for (index in page.items.indices) {
            if (admittedCount >= remaining) break
            if (payloadBudgetEnabled && costs[index] > maxPayloadBytes - bytes) {
                byteOmission = true
                break
            }
            bytes += costs[index]
            admittedCount += 1
        }
        val admitted = page.items.take(admittedCount)
        val truncated = admitted.size < page.items.size
        val combined = storedItems + admitted
        val more = page.hasMore || truncated
        val capped = combined.size == maxItems && more
        val bytesCapped = byteOmission || (payloadBudgetEnabled && bytes == maxPayloadBytes && more)
        // Never advance the cursor past an omitted row, even when none fit.
        val retainedCursor = combined.lastOrNull()?.let(idOf) ?: nextBeforeId
        return ArtifactPageChain(artifactId, limit, maxItems, maxPayloadBytes, rowSizeOf,
            idOf, artifactIdOf, combined, page.snapshotMaxId, if (more) retainedCursor else null,
            more, true, capped, bytes, bytesCapped)
    }
}
