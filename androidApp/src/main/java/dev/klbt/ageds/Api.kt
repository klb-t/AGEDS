package dev.klbt.ageds

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import dev.klbt.ageds.core.*
import dev.klbt.ageds.core.Annotation as EvidenceAnnotation
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
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

class EvidenceApi(baseUrl: String) {
    private var baseUrl: String = baseUrl.trimEnd('/')
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val client = HttpClient(OkHttp) {
        expectSuccess = true
        install(ContentNegotiation) { json(json) }
    }

    fun setBaseUrl(value: String) { baseUrl = value.trimEnd('/') }
    fun close() { client.close() }

    suspend fun artifacts(): List<ArtifactSummary> = client.get("$baseUrl/api/artifacts").body()
    suspend fun transcriptVersions(artifactId: Long): List<TranscriptVersion> =
        client.get("$baseUrl/api/artifacts/$artifactId/transcripts").body()

    suspend fun transcript(artifactId: Long, derivedTextId: Long? = null): Transcript? =
        client.get("$baseUrl/api/artifacts/$artifactId/transcript") {
            if (derivedTextId != null) parameter("derived_text_id", derivedTextId)
        }.body()

    suspend fun citations(artifactId: Long): List<Citation> =
        client.get("$baseUrl/api/artifacts/$artifactId/citations").body()

    suspend fun createCitation(artifactId: Long, request: CitationCreate): Citation =
        client.post("$baseUrl/api/artifacts/$artifactId/citations") {
            contentType(ContentType.Application.Json)
            setBody(request)
        }.body()

    fun contentUrl(artifactId: Long): String = "$baseUrl/api/artifacts/$artifactId/content"
    suspend fun annotations(artifactId: Long): List<EvidenceAnnotation> = client.get("$baseUrl/api/artifacts/$artifactId/annotations").body()

    suspend fun queueTranscription(artifactId: Long, priority: Int): QueueResult =
        client.post("$baseUrl/api/artifacts/$artifactId/transcribe") { parameter("priority", priority) }.body()

    suspend fun annotate(artifactId: Long, annotation: AnnotationCreate ): EvidenceAnnotation =
        client.post("$baseUrl/api/artifacts/$artifactId/annotations") {
            contentType(ContentType.Application.Json)
            setBody(annotation)
        }.body()

    suspend fun uploadAudio(resolver: ContentResolver, uri: Uri, sourcePath: String? = null): UploadResult {
        val name = queryName(resolver, uri) ?: "recording"
        val transportName = name.replace('"', '_').replace('\\', '_').replace('\r', '_').replace('\n', '_')
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        return client.submitFormWithBinaryData(
            url = "$baseUrl/api/artifacts/upload",
            formData = formData {
                append("source_label", "AGEDS Android import")
                append("source_locator", uri.toString())
                append("metadata_json", buildJsonObject {
                    put("client", "AGEDS Android")
                    put("locator_kind", "android_saf_uri")
                    put("client_original_name", name)
                    if (sourcePath != null) put("client_relative_path", sourcePath)
                }.toString())
                append("file", InputProvider {
                    resolver.openInputStream(uri)?.asInput() ?: error("Cannot open $uri")
                }, Headers.build {
                    append(HttpHeaders.ContentType, mime)
                    append(HttpHeaders.ContentDisposition, "filename=\"$transportName\"")
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
