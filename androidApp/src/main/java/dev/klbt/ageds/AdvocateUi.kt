package dev.klbt.ageds

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException

/** Server-derived advocate state for one account; cleared with the account. */
class AdvocateState {
    val status = mutableStateOf<AdvocateStatus?>(null)
    val conversations = mutableStateListOf<AdvocateConversation>()
    val current = mutableStateOf<String?>(null)
    val turns = mutableStateListOf<AdvocateTurn>()
    val basis = mutableStateMapOf<String, List<AdvocateBasisEntry>>()
    val opened = mutableStateMapOf<String, String>()
    val error = mutableStateOf<String?>(null)
    val busy = mutableStateOf(false)

    fun clear() {
        status.value = null; conversations.clear(); current.value = null; turns.clear()
        basis.clear(); opened.clear(); error.value = null; busy.value = false
    }

    private suspend fun guarded(onUnauthorized: () -> Unit, block: suspend () -> Unit) {
        busy.value = true
        try { block(); error.value = null }
        catch (t: Throwable) {
            if (t is CancellationException) throw t
            val status = (t as? io.ktor.client.plugins.ClientRequestException)?.response?.status?.value
            when (status) {
                401 -> { onUnauthorized(); error.value = "Token jest nieprawidłowy albo został cofnięty." }
                403, 404 -> error.value = "To konto nie ma dostępu do rzecznika albo tej rozmowy."
                409 -> error.value = "Rzecznik nie jest jeszcze skonfigurowany na serwerze."
                else -> error.value = "Rzecznik: ${t.message}"
            }
        } finally { busy.value = false }
    }

    suspend fun load(api: EvidenceApi, onUnauthorized: () -> Unit) = guarded(onUnauthorized) {
        status.value = api.advocateStatus()
        conversations.clear(); conversations.addAll(api.advocateConversations())
        conversations.firstOrNull()?.let { open(api, it.uid) }
    }

    suspend fun open(api: EvidenceApi, uid: String) {
        current.value = uid
        val history = api.advocateHistory(uid)
        turns.clear(); turns.addAll(history.turns)
    }

    suspend fun openGuarded(api: EvidenceApi, uid: String, onUnauthorized: () -> Unit) = guarded(onUnauthorized) { open(api, uid) }

    suspend fun ask(api: EvidenceApi, question: String, onUnauthorized: () -> Unit) = guarded(onUnauthorized) {
        val uid = current.value ?: api.advocateStart().also { conversations.add(0, it) }.uid.also { current.value = it }
        val answer = api.advocateAsk(uid, question)
        answer.basis?.let { basis[answer.answerUid] = it }
        turns.add(AdvocateTurn(turnUid = answer.answerUid, question = question, answers = listOf(answer)))
    }

    suspend fun cite(api: EvidenceApi, answer: AdvocateAnswer, ref: String, onUnauthorized: () -> Unit) = guarded(onUnauthorized) {
        if (answer.answerUid !in basis) basis[answer.answerUid] = answer.basis ?: api.advocateBasis(answer.answerUid).entries
        val key = "${answer.answerUid}/$ref"
        if (opened[answer.answerUid] == key) opened.remove(answer.answerUid) else opened[answer.answerUid] = key
    }
}

@Composable
fun AdvocatePane(state: AdvocateState, onAsk: (String) -> Unit, onOpen: (String) -> Unit,
                 onCite: (AdvocateAnswer, String) -> Unit, modifier: Modifier = Modifier) {
    var question by remember { mutableStateOf("") }
    val status = state.status.value
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        state.error.value?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (status != null) {
            Text(if (status.provider != null) "Odpowiedzi formułuje model ${status.provider.model} (Google Vertex) na podstawie fragmentów; każde twierdzenie ma etykietę i cytaty."
                 else "Model językowy nie jest włączony: rzecznik pokazuje wyszukane fragmenty materiału z cytatami.",
                 style = MaterialTheme.typography.bodySmall)
            Text(AdvocateProjection.ownerVisibility(status.ownerVisibility), style = MaterialTheme.typography.bodySmall)
            Text("Etykiety opisują rodzaj twierdzenia, nie prawdopodobieństwo. Transkrypcje mogą zawierać błędy rozpoznawania mowy.",
                 style = MaterialTheme.typography.labelSmall)
        }
        if (state.conversations.size > 1) {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                state.conversations.take(4).forEach { conversation ->
                    FilterChip(selected = state.current.value == conversation.uid, onClick = { onOpen(conversation.uid) },
                               label = { Text(conversation.title ?: conversation.createdAt.take(16).replace('T', ' ')) })
                }
            }
        }
        LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            items(state.turns, key = { it.turnUid }) { turn ->
                Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(turn.question, fontWeight = FontWeight.SemiBold)
                    turn.answers.forEach { answer -> AnswerView(state, answer, status?.provider?.model, onCite) }
                } }
            }
        }
        OutlinedTextField(question, { question = it.take(4000) }, label = { Text("Pytanie") }, modifier = Modifier.fillMaxWidth())
        Button(onClick = { onAsk(question.trim()); question = "" }, enabled = !state.busy.value && question.isNotBlank() && status?.canChat == true,
               modifier = Modifier.fillMaxWidth()) { Text("Zapytaj") }
    }
}

@Composable
private fun AnswerView(state: AdvocateState, answer: AdvocateAnswer, providerModel: String?, onCite: (AdvocateAnswer, String) -> Unit) {
    Text(AdvocateProjection.mode(answer, providerModel), style = MaterialTheme.typography.labelSmall)
    AdvocateProjection.fallback(answer)?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.tertiary) }
    if (!answer.unavailable && answer.claims.isEmpty()) Text("Nie znaleziono pasujących fragmentów w udostępnionym materiale.")
    answer.claims.forEach { claim ->
        Text("[${AdvocateProjection.label(claim.status)}] ${claim.text}")
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            claim.citations.forEach { ref ->
                AssistChip(onClick = { onCite(answer, ref) }, label = { Text(ref) })
            }
        }
        val opened = state.opened[answer.answerUid]
        claim.citations.firstOrNull { opened == "${answer.answerUid}/$it" }?.let { ref ->
            val entry = AdvocateProjection.resolve(ref, state.basis[answer.answerUid].orEmpty())
            Card(Modifier.fillMaxWidth().clickable { onCite(answer, ref) }) { Column(Modifier.padding(8.dp)) {
                if (entry.unavailable) Text("$ref: materiał nie jest już dostępny dla tego konta.", color = MaterialTheme.colorScheme.error)
                else {
                    Text(AdvocateProjection.heading(entry), style = MaterialTheme.typography.labelSmall)
                    Text(entry.text.orEmpty())
                    entry.artifact?.sha256?.let { Text("SHA-256 materiału: ${it.take(16)}…", style = MaterialTheme.typography.labelSmall) }
                    entry.derivedTextId?.let { Text("wersja tekstu #$it", style = MaterialTheme.typography.labelSmall) }
                    entry.note?.let { Text(it, style = MaterialTheme.typography.labelSmall) }
                }
            } }
        }
    }
}
