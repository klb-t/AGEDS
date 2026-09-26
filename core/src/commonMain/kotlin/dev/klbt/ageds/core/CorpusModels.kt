package dev.klbt.ageds.core

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerialName
import kotlinx.serialization.json.JsonObject

@Serializable
data class CorpusSource(
    val title: String = "",
    val driveSpreadsheetId: String? = null,
)

@Serializable
data class CorpusPreset(
    val id: String,
    val label: String,
    val description: String = "",
    val phones: List<String> = emptyList(),
    val emails: List<String> = emptyList(),
    @SerialName("email_groups") val emailGroups: List<String> = emptyList(),
    @SerialName("email_domains") val emailDomains: List<String> = emptyList(),
    @SerialName("label_keywords") val labelKeywords: List<String> = emptyList(),
    @SerialName("short_senders") val shortSenders: List<String> = emptyList(),
)

@Serializable
data class EmailIdentity(
    val email: String,
    val label: String? = null,
    val group: String? = null,
    val source: String? = null,
)

@Serializable
data class CorpusContact(
    val phone: String? = null,
    val rawVariants: String? = null,
    val backupName: String? = null,
    val label: String? = null,
    val labelSource: String? = null,
    val calls: Int = 0,
    val incoming: Int = 0,
    val outgoing: Int = 0,
    val missed: Int = 0,
    val callSeconds: Int = 0,
    val sms: Int = 0,
    val smsReceived: Int = 0,
    val smsSent: Int = 0,
    val mms: Int = 0,
    val interactions: Int = 0,
    val recordings: Int = 0,
    val publicId: String? = null,
    val publicConfidence: String? = null,
    val notes: String? = null,
)

@Serializable
data class CorpusCall(
    val phone: String? = null,
    val rawNumber: String? = null,
    val contact: String? = null,
    val start: String? = null,
    val type: String? = null,
    val durationSec: Int = 0,
    val end: String? = null,
)

@Serializable
data class CorpusSms(
    val phone: String? = null,
    val rawSender: String? = null,
    val contact: String? = null,
    val at: String? = null,
    val type: String? = null,
    val text: String = "",
)

@Serializable
data class CorpusRecording(
    val folder: String? = null,
    val name: String,
    val driveUrl: String? = null,
    val sizeBytes: Long = 0,
    val mime: String? = null,
    val filenamePhone: String? = null,
    val phone: String? = null,
    val contact: String? = null,
    val callStart: String? = null,
    val callDurationSec: Double? = null,
    val audioDurationSec: Double? = null,
    val confidence: String? = null,
    val note: String? = null,
    val driveFileId: String? = null,
    val resolvedTime: String? = null,
    val matchBasis: String? = null,
)

@Serializable
data class CorpusSeed(
    val schemaVersion: Int = 1,
    val generatedAt: String? = null,
    val source: CorpusSource = CorpusSource(),
    val presets: List<CorpusPreset> = emptyList(),
    val emailIdentities: List<EmailIdentity> = emptyList(),
    val contacts: List<CorpusContact> = emptyList(),
    val calls: List<CorpusCall> = emptyList(),
    val sms: List<CorpusSms> = emptyList(),
    val mms: List<CorpusSms> = emptyList(),
    val recordings: List<CorpusRecording> = emptyList(),
    val shortSenders: List<JsonObject> = emptyList(),
)
