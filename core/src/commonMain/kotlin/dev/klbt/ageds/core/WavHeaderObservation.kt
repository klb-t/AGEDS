package dev.klbt.ageds.core

import kotlinx.serialization.Serializable

/** Header declarations only: neither decoded audio nor a complete-container integrity claim. */
@Serializable
data class WavHeaderObservation(
    val status: String = "partial",
    val coverage: String = "header_prefix_only",
    val bytesInspected: Int = 0,
    val prefixLimitBytes: Int = 65536,
    val endOfInput: Boolean = false,
    val providerSizeBytes: Long? = null,
    val riffDeclaredBytes: Long? = null,
    val formatCode: Int? = null,
    val channels: Int? = null,
    val sampleRateHz: Long? = null,
    val byteRate: Long? = null,
    val blockAlignBytes: Int? = null,
    val bitsPerSample: Int? = null,
    val dataDeclaredBytes: Long? = null,
    val declaredDurationSec: Double? = null,
    val durationBasis: String? = null,
    val bodyValidated: Boolean = false,
    val issues: List<ScanIssue> = emptyList(),
)
