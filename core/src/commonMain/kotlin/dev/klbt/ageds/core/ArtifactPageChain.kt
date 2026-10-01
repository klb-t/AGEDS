package dev.klbt.ageds.core

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
    private val idOf: (T) -> Long,
    private val artifactIdOf: (T) -> Long?,
    private val storedItems: List<T>,
    val snapshotMaxId: Long?,
    val nextBeforeId: Long?,
    val hasMore: Boolean,
    val isLoaded: Boolean,
    val reachedClientLimit: Boolean,
) {
    constructor(
        artifactId: Long,
        limit: Int = 100,
        maxItems: Int = 1000,
        idOf: (T) -> Long,
        artifactIdOf: (T) -> Long?,
    ) : this(artifactId, limit, maxItems, idOf, artifactIdOf, emptyList(), null, null, false, false, false)

    init {
        require(artifactId > 0) { "Artifact ID must be positive" }
        require(limit in 1..100) { "Page limit must be between 1 and 100" }
        require(maxItems in 1..1000) { "Client maximum must be between 1 and 1000" }
    }

    val items: List<T> get() = storedItems.toList()
    val canLoadMore: Boolean get() = !reachedClientLimit && (!isLoaded || hasMore)

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
        val remaining = maxItems - storedItems.size
        val admitted = page.items.take(remaining)
        val truncated = admitted.size < page.items.size
        val combined = storedItems + admitted
        val more = page.hasMore || truncated
        val capped = combined.size == maxItems && more
        return ArtifactPageChain(artifactId, limit, maxItems, idOf, artifactIdOf, combined,
            page.snapshotMaxId, if (more) idOf(combined.last()) else null, more, true, capped)
    }
}
