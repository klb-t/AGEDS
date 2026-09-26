package dev.klbt.ageds

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import dev.klbt.ageds.core.CorpusSeed
import kotlinx.serialization.json.Json
import java.io.File

class CorpusStore(private val context: Context) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val cacheFile = File(context.filesDir, "ageds-corpus-seed.json")

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

    fun indexRecordingTree(treeUri: Uri, expectedNames: Set<String>): Map<String, Uri> {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: error("Nie mogę otworzyć folderu nagrań")
        val found = LinkedHashMap<String, Uri>()
        fun walk(node: DocumentFile) {
            if (node.isDirectory) {
                node.listFiles().forEach(::walk)
                return
            }
            val name = node.name ?: return
            if (name in expectedNames && name !in found) found[name] = node.uri
        }
        walk(root)
        return found
    }
}
