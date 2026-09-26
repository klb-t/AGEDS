package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import dev.klbt.ageds.core.CorpusSeed
import kotlinx.serialization.json.Json
import java.io.File
import java.util.ArrayDeque

data class RecordingScan(
    val found: Map<String, Uri>,
    val scannedFiles: Int,
    val scannedDirectories: Int,
    val sampleNames: List<String>,
)

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

    fun indexRecordingTree(treeUri: Uri, expectedNames: Set<String>): RecordingScan {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: error("Nie mogę otworzyć wskazanego folderu")
        val found = LinkedHashMap<String, Uri>()
        val samples = ArrayList<String>()
        val queue = ArrayDeque<DocumentFile>()
        queue.add(root)
        var scannedFiles = 0
        var scannedDirectories = 0

        while (queue.isNotEmpty()) {
            val node = queue.removeFirst()
            if (node.isDirectory) {
                scannedDirectories++
                val children = runCatching { node.listFiles().toList() }
                    .getOrElse { throw IllegalStateException("Nie mogę odczytać folderu ${node.name ?: node.uri}: ${it.message}", it) }
                children.forEach(queue::addLast)
                continue
            }
            scannedFiles++
            val name = node.name ?: continue
            if (samples.size < 12) samples += name
            if (name in expectedNames && name !in found) found[name] = node.uri
        }

        saveRecordingTree(treeUri)
        return RecordingScan(found, scannedFiles, scannedDirectories, samples)
    }
}
