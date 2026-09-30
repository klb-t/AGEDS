package dev.klbt.ageds.core

import kotlinx.serialization.Serializable

/** Bounds are processing budgets, not a claim that a provider's tree is complete. */
@Serializable
data class SourceScanLimits(
    val maxFiles: Int = 1000,
    val maxDirectories: Int = 250,
    val maxDepth: Int = 12,
    val maxFileBytes: Long = 8L * 1024 * 1024,
    val maxTotalBytes: Long = 64L * 1024 * 1024,
    val maxRowsPerFile: Int = 2000,
    val maxCellsPerFile: Int = 20000,
    val maxCellChars: Int = 16000,
    val maxArchiveEntries: Int = 256,
    val maxExpandedBytes: Int = 16 * 1024 * 1024,
) {
    init {
        require(maxFiles in 1..10000 && maxDirectories in 1..2000 && maxDepth in 0..64)
        require(maxFileBytes in 1..64L * 1024 * 1024 && maxTotalBytes in 1..512L * 1024 * 1024)
        require(maxRowsPerFile in 1..20000 && maxCellsPerFile in 1..100000 && maxCellChars in 1..100000)
        require(maxArchiveEntries in 1..2000 && maxExpandedBytes in 1..64 * 1024 * 1024)
    }
}

@Serializable
data class ScanIssue(val code: String, val message: String, val locator: String? = null)

/** raw is a CSV lexical token, XLSX stored value or XLS BIFF payload hex; value is a decoded projection. */
@Serializable
data class SourceCell(val column: Int, val raw: String, val value: String, val formula: String? = null,
    val sourceReference: String? = null, val sourceType: String? = null)

@Serializable
data class SourceRow(val locator: String, val cells: List<SourceCell>)

/** Inference is a parsing hypothesis, not source-supplied dialect metadata. */
@Serializable
data class SourceTextFormat(
    val encoding: String,
    val encodingBasis: String,
    val bomBytes: Int,
    val delimiter: String,
    val delimiterBasis: String,
    val delimiterCandidates: List<String> = emptyList(),
    val delimiterAmbiguous: Boolean = false,
    val sampledRecords: Int = 0,
    val sampleTruncated: Boolean = false,
)

@Serializable
data class ScannedSourceFile(
    val uri: String,
    val name: String,
    val relativePath: String,
    val mime: String? = null,
    val sizeBytes: Long? = null,
    val kind: String = "inventory",
    val sha256: String? = null,
    val coverage: String = "inventory_only",
    val rows: List<SourceRow> = emptyList(),
    val issues: List<ScanIssue> = emptyList(),
    val audioDurationSec: Double? = null,
    val textFormat: SourceTextFormat? = null,
)

@Serializable
data class SourceScanResult(
    val schemaVersion: Int = 1,
    val rootUri: String,
    val scannedAt: String,
    val files: List<ScannedSourceFile>,
    val scannedDirectories: Int,
    val coverage: String,
    val issues: List<ScanIssue> = emptyList(),
    val limits: SourceScanLimits = SourceScanLimits(),
    val bytesRead: Long = 0,
)

data class ParsedSourceRows(val rows: List<SourceRow>, val issues: List<ScanIssue>, val textFormat: SourceTextFormat? = null)
