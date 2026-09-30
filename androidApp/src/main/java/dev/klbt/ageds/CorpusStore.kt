package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import dev.klbt.ageds.core.CorpusSeed
import kotlinx.serialization.json.Json
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import java.io.File
import java.util.ArrayDeque

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
    private val cacheFile = File(context.filesDir, "ageds-corpus-seed.json")
    private val prefs = context.getSharedPreferences("ageds-corpus", Context.MODE_PRIVATE)

    fun loadCached(): CorpusSeed? = runCatching {
        if (!cacheFile.exists()) null else json.decodeFromString<CorpusSeed>(cacheFile.readText())
    }.getOrNull()

    fun import(uri: Uri): CorpusSeed {
        val text = context.contentResolver.openInputStream(uri)?.bufferedReader()?.use { it.readText() }
            ?: error("Nie mogę otworzyć pakietu korpusu")
        val seed = json.decodeFromString<CorpusSeed>(text)
        require(seed.contacts.isNotEmpty()) { "Pakiet nie zawiera katalogu numerów" }
        cacheFile.writeText(text)
        return seed
    }

    fun saveRecordingTree(uri: Uri) {
        prefs.edit().putString("recordingTree", uri.toString()).apply()
    }

    fun savedRecordingTree(): Uri? = prefs.getString("recordingTree", null)?.let(Uri::parse)

    suspend fun indexRecordingTree(treeUri: Uri, expectedNames: Set<String>): RecordingScan {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: error("Nie mogę otworzyć wskazanego folderu")
        val found = LinkedHashMap<String, MutableList<RecordingCandidate>>()
        val samples = ArrayList<String>()
        val queue = ArrayDeque<Pair<DocumentFile, String>>()
        val visited = HashSet<String>()
        queue.add(root to "")
        var scannedFiles = 0
        var scannedDirectories = 0

        while (queue.isNotEmpty()) {
            currentCoroutineContext().ensureActive()
            val (node, path) = queue.removeFirst()
            if (!visited.add(node.uri.toString())) continue
            if (node.isDirectory) {
                scannedDirectories++
                val children = runCatching { node.listFiles().toList() }
                    .getOrElse { throw IllegalStateException("Nie mogę odczytać folderu ${node.name ?: node.uri}: ${it.message}", it) }
                children.forEach { child ->
                    queue.addLast(child to listOf(path, child.name ?: child.uri.toString()).filter { it.isNotEmpty() }.joinToString("/"))
                }
                continue
            }
            scannedFiles++
            val name = node.name ?: continue
            if (samples.size < 12) samples += name
            if (name in expectedNames) found.getOrPut(name) { ArrayList() }
                .add(RecordingCandidate(name, node.uri, path, node.length()))
        }

        currentCoroutineContext().ensureActive()
        saveRecordingTree(treeUri)
        return RecordingScan(found, scannedFiles, scannedDirectories, samples)
    }
}
