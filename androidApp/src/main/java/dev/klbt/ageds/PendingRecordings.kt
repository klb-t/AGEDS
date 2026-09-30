package dev.klbt.ageds

import android.content.Context
import android.content.Intent
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.util.UUID

@Serializable
data class PendingRecording(val uri: String, val name: String, val locator: String)

/** Private metadata only: originals remain at their read-only source locators. */
class PendingRecordingStore(private val context: Context) {
    private val json = Json { ignoreUnknownKeys = true }
    private fun file(id: String): File {
        require(UUID.fromString(id).toString() == id) { "Nieprawidłowy identyfikator wyboru" }
        return File(context.cacheDir, "ageds-selection-$id.json")
    }

    fun prepare(candidates: List<RecordingCandidate>): Intent {
        val id = UUID.randomUUID().toString()
        val selection = candidates.distinctBy { it.uri.toString() }
            .map { PendingRecording(it.uri.toString(), it.name, it.relativePath) }
        require(selection.isNotEmpty()) { "Brak dostępnych plików w wyborze" }
        file(id).writeText(json.encodeToString(selection))
        return Intent(context, MainActivity::class.java).putExtra(EXTRA_SELECTION, id)
    }

    fun read(id: String): List<PendingRecording> = json.decodeFromString(file(id).readText())

    companion object { const val EXTRA_SELECTION = "dev.klbt.ageds.PENDING_SELECTION" }
}
