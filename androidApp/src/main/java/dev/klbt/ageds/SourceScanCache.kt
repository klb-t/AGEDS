package dev.klbt.ageds

import android.content.Context
import dev.klbt.ageds.core.SourceScanResult
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/** A historical scan, never an assertion that source URIs still exist or retain their bytes. */
internal class SourceScanCache(context: Context) {
    private val json = Json { ignoreUnknownKeys = true }
    private val cache = BoundedMetadataCache(File(context.filesDir, "ageds-source-scan.json"), 4 * 1024 * 1024)

    fun read(): SourceScanResult? = cache.read()?.let { raw ->
        json.decodeFromString<SourceScanResult>(raw).also {
            require(it.schemaVersion == 1) { "Nieobsługiwana wersja cache skanu" }
        }
    }

    @Synchronized
    fun write(result: SourceScanResult, beforeCommit: () -> Unit) = cache.write(json.encodeToString(result), beforeCommit)
}
