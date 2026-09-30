package dev.klbt.ageds

import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** App-private metadata only. Failed or oversized replacements leave the old cache intact. */
internal class BoundedMetadataCache(private val file: File, private val maxBytes: Int) {
    init { require(maxBytes > 0) }

    fun read(): String? {
        if (!file.exists()) return null
        require(file.length() <= maxBytes) { "Cache przekracza limit $maxBytes B" }
        val bytes = file.inputStream().use { input ->
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                require(output.size().toLong() + count <= maxBytes) { "Cache przekracza limit $maxBytes B" }
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        }
        return Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
    }

    fun write(text: String, beforeCommit: () -> Unit = {}) {
        beforeCommit()
        require(text.length <= maxBytes) { "Metadane przekraczają limit $maxBytes B" }
        val bytes = text.toByteArray(Charsets.UTF_8)
        require(bytes.size <= maxBytes) { "Metadane przekraczają limit $maxBytes B" }
        val parent = requireNotNull(file.parentFile)
        check(parent.isDirectory || parent.mkdirs()) { "Nie mogę przygotować prywatnego cache" }
        val temporary = File.createTempFile(file.name + ".", ".tmp", parent)
        try {
            temporary.outputStream().use { stream -> stream.write(bytes); stream.fd.sync() }
            // No non-atomic fallback: a filesystem without atomic move keeps the previous version.
            beforeCommit()
            Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } finally { temporary.delete() }
    }
}
