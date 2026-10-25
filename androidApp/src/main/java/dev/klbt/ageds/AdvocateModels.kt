package dev.klbt.ageds

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

@Serializable
data class AdvocateProvider(val name: String, val model: String)

@Serializable
data class AdvocateStatus(
    val principal: String,
    @SerialName("auth_mode") val authMode: String,
    @SerialName("can_chat") val canChat: Boolean,
    val provider: AdvocateProvider? = null,
    @SerialName("owner_visibility") val ownerVisibility: String = "none",
)

@Serializable
data class AdvocateConversation(
    val uid: String,
    val title: String? = null,
    @SerialName("created_at") val createdAt: String = "",
)

@Serializable
data class AdvocateClaim(val text: String, val status: String, val citations: List<String> = emptyList())

@Serializable
data class AdvocateArtifactRef(val id: Long, val uid: String? = null, val sha256: String? = null, val name: String = "")

/** A pinned passage. Quoted text is stored derived text (ASR, import), not verified truth. */
@Serializable
data class AdvocateBasisEntry(
    val ref: String,
    val kind: String = "",
    val unavailable: Boolean = false,
    val text: String? = null,
    val artifact: AdvocateArtifactRef? = null,
    @SerialName("derived_text_id") val derivedTextId: Long? = null,
    @SerialName("start_ms") val startMs: Long? = null,
    @SerialName("end_ms") val endMs: Long? = null,
    val model: String? = null,
    val note: String? = null,
)

@Serializable
data class AdvocateAnswer(
    @SerialName("answer_uid") val answerUid: String,
    val mode: String = "",
    val claims: List<AdvocateClaim> = emptyList(),
    val basis: List<AdvocateBasisEntry>? = null,
    val verification: JsonObject? = null,
    val unavailable: Boolean = false,
)

@Serializable
data class AdvocateTurn(
    @SerialName("turn_uid") val turnUid: String,
    val question: String,
    val answers: List<AdvocateAnswer> = emptyList(),
)

@Serializable
data class AdvocateHistory(@SerialName("conversation_uid") val conversationUid: String, val turns: List<AdvocateTurn>)

@Serializable
data class AdvocateBasis(@SerialName("answer_uid") val answerUid: String, val entries: List<AdvocateBasisEntry>)

@Serializable
data class AdvocateQuestion(val question: String)

@Serializable
data class AdvocateNewConversation(val title: String? = null)
