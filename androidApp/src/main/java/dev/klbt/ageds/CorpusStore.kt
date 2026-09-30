package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import dev.klbt.ageds.core.CorpusSeed
import kotlinx.serialization.json.Json
import java.io.File

data class RecordingScan(
    val found: Map<String, List<RecordingCandidate>>,
    val scannedFiles: Int,
    val scannedDirectories: Int,
    val sampleNames: List<String>,
)

/** A filename is an observation, never the identity of a physical recording. */
data class RecordingCandidate(val name: String, val uri: Uri, val relativePath: String, val sizeBytes: Long)

class CorpusStore(private val context: Context) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val cache = BoundedMetadataCache(File(context.filesDir, "ageds-corpus-seed.json"), MAX_SEED_BYTES)
    private val prefs = context.getSharedPreferences("ageds-corpus", Context.MODE_PRIVATE)

    fun loadCached(): CorpusSeed? = cache.read()?.let { json.decodeFromString<CorpusSeed>(it) }

    fun import(uri: Uri): CorpusSeed {
        val text = context.contentResolver.openInputStream(uri)?.bufferedReader(Charsets.UTF_8)?.use { reader ->
            val output = StringBuilder()
            val chunk = CharArray(8192)
            while (true) {
                val read = reader.read(chunk)
                if (read < 0) break
                require(output.length.toLong() + read <= MAX_SEED_BYTES) { "Pakiet przekracza limit 16 MiB" }
                output.append(chunk, 0, read)
            }
            output.toString()
        } ?: error("Nie mogę otworzyć pakietu korpusu")
        val seed = json.decodeFromString<CorpusSeed>(text)
        require(seed.contacts.isNotEmpty()) { "Pakiet nie zawiera katalogu numerów" }
        cache.write(text)
        return seed
    }

    fun saveRecordingTree(uri: Uri) {
        prefs.edit().putString("recordingTree", uri.toString()).apply()
    }

    fun savedRecordingTree(): Uri? = prefs.getString("recordingTree", null)?.let(Uri::parse)

    companion object { private const val MAX_SEED_BYTES = 16 * 1024 * 1024 }
}
