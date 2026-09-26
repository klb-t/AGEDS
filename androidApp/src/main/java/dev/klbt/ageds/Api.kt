package dev.klbt.ageds

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import dev.klbt.ageds.core.*
import io.ktor.client.*
import io.ktor.client.call.*
import io.ktor.client.engine.okhttp.*
import io.ktor.client.plugins.contentnegotiation.*
import io.ktor.client.request.*
import io.ktor.client.request.forms.*
import io.ktor.http.*
import io.ktor.serialization.kotlinx.json.*
import io.ktor.utils.io.streams.*
import kotlinx.serialization.json.Json

class EvidenceApi(private var baseUrl: String) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val client = HttpClient(OkHttp) {
        install(ContentNegotiation) { json(json) }
    }

    fun setBaseUrl(value: String) { baseUrl = value.trimEnd('/') }

    suspend fun artifacts(): List<ArtifactSummary> = client.get("$baseUrl/api/artifacts").body()
    suspend fun transcript(artifactId: Long): Transcript? = client.get("$baseUrl/api/artifacts/$artifactId/transcript").body()
    suspend fun annotations(artifactId: Long): List<Annotation> = client.get("$baseUrl/api/artifacts/$artifactId/annotations").body()

    suspend fun queueTranscription(artifactId: Long, priority: Int): QueueResult =
        client.post("$baseUrl/api/artifacts/$artifactId/transcribe") { parameter("priority", priority) }.body()

    suspend fun annotate(artifactId: Long, annotation: AnnotationCreate): Annotation =
        client.post("$baseUrl/api/artifacts/$artifactId/annotations") {
            contentType(ContentType.Application.Json)
            setBody(annotation)
        }.body()

    suspend fun uploadAudio(resolver: ContentResolver, uri: Uri): UploadResult {
        val name = queryName(resolver, uri) ?: "recording"
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        return client.submitFormWithBinaryData(
            url = "$baseUrl/api/artifacts/upload",
            formData = formData {
                append("source_label", "AGEDS Android import")
                append("file", InputProvider {
                    resolver.openInputStream(uri)?.asInput() ?: error("Cannot open $uri")
                }, Headers.build {
                    append(HttpHeaders.ContentType, mime)
                    append(HttpHeaders.ContentDisposition, "filename=\"${name.replace("\"", "_")}\"")
                })
            }
        ).body()
    }

    private fun queryName(resolver: ContentResolver, uri: Uri): String? {
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) return c.getString(0)
        }
        return uri.lastPathSegment
    }
}
