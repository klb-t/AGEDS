package dev.klbt.ageds

import dev.klbt.ageds.core.ScanIssue
import dev.klbt.ageds.core.WavHeaderObservation
import dev.klbt.ageds.core.WavHeaderProbe
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.CancellationException

/** Full-file hashing and bounded header inspection have independent coverage. */
data class SourceWavReadResult(
    val bytesRead: Long,
    val sha256: String?,
    val complete: Boolean,
    val wavHeader: WavHeaderObservation,
    val issues: List<ScanIssue>,
)

/** Read-only stream policy shared by SAF and JVM acceptance; owns/closes input. */
object SourceWavReader {
    fun read(input: InputStream, reportedSize: Long?, maxFileBytes: Long,
             remainingTotalBytes: Long, checkCancelled: () -> Unit = {}): SourceWavReadResult {
        val prefix = ByteArrayOutputStream()
        val digest = MessageDigest.getInstance("SHA-256")
        val issues = mutableListOf<ScanIssue>()
        var total = 0L
        var complete = false
        var headerOnly = false
        try {
            input.use {
                require(maxFileBytes >= 0 && remainingTotalBytes >= 0) { "Negative WAV read budget" }
                require(reportedSize == null || reportedSize >= 0) { "Negative provider size" }
                checkCancelled()
                val fullCap = minOf(maxFileBytes, remainingTotalBytes)
                headerOnly = reportedSize != null && reportedSize > fullCap
                val readCap = if (headerOnly) minOf(fullCap, WavHeaderProbe.MAX_PREFIX_BYTES.toLong()) else fullCap
                val buffer = ByteArray(8192)
                while (total < readCap) {
                    checkCancelled()
                    val requested = minOf(buffer.size.toLong(), readCap - total).toInt()
                    val count = input.read(buffer, 0, requested)
                    if (count == -1) { complete = true; break }
                    if (count <= 0 || count > requested) throw IOException("Provider stream made invalid progress")
                    total += count
                    digest.update(buffer, 0, count)
                    val retained = minOf(count, WavHeaderProbe.MAX_PREFIX_BYTES - prefix.size())
                    if (retained > 0) prefix.write(buffer, 0, retained)
                }
                checkCancelled()
            }
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            complete = false
            issues += ScanIssue("read_failed", "WAV read failed: ${error.message?.take(200)}")
        }
        if (!complete && issues.none { it.code == "read_failed" }) {
            issues += ScanIssue("bytes_limit", if (headerOnly)
                "Only a bounded WAV header prefix was read; no complete-file hash" else
                "WAV read budget exhausted before observed EOF; no complete-file hash")
        }
        if (complete && reportedSize != null && reportedSize != total) {
            issues += ScanIssue("size_changed", "Provider size differs from bytes read; source may have changed")
        }
        val bytes = prefix.toByteArray()
        // EOF belongs to the supplied prefix only when it contains every read byte.
        // A complete hash of a larger file does not validate its unseen chunk layout.
        var header = WavHeaderProbe.probe(bytes, reportedSize, complete && total == bytes.size.toLong())
        if (complete && header.riffDeclaredBytes != null && header.riffDeclaredBytes != total) {
            header = header.copy(status = "size_mismatch", declaredDurationSec = null, durationBasis = null,
                issues = header.issues + ScanIssue("wav_stream_size_mismatch",
                    "RIFF declared size differs from the $total bytes read through observed EOF"))
        }
        return SourceWavReadResult(total, if (complete) digest.digest().joinToString("") { "%02x".format(it) } else null,
            complete, header, issues)
    }
}
