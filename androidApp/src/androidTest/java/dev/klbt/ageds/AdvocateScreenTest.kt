package dev.klbt.ageds

import android.content.Context
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * The advocate screen on a device against a synthetic backend started by the CI job
 * (server/tests/browser_advocate_fixture.py, tokens mode). Arguments: agedsServer,
 * agedsToken, agedsHidden. Without them the test is skipped, never faked.
 */
@RunWith(AndroidJUnit4::class)
class AdvocateScreenTest {
    private val arguments = InstrumentationRegistry.getArguments()
    private val server: String? = arguments.getString("agedsServer")
    private val token: String? = arguments.getString("agedsToken")
    private val hidden: String = arguments.getString("agedsHidden") ?: "CANARY"

    @get:Rule val rule = createEmptyComposeRule()

    /** Settings are written before the activity (and its ViewModel) exist. */
    private fun launch(tokenValue: String): ActivityScenario<MainActivity> {
        val target = InstrumentationRegistry.getInstrumentation().targetContext
        target.getSharedPreferences("ageds-evidence", Context.MODE_PRIVATE).edit().clear()
            .putString("serverUrl", server).putString("accessToken", tokenValue).commit()
        return ActivityScenario.launch(MainActivity::class.java)
    }

    @Before fun requireBackend() = assumeTrue("synthetic backend arguments missing", server != null && token != null)

    private fun waitForText(text: String, timeoutMs: Long = 20_000) =
        rule.waitUntil(timeoutMs) { rule.onAllNodesWithText(text, substring = true).fetchSemanticsNodes().isNotEmpty() }

    @Test fun askShowsLabelledQuotesAndThePinnedBasis() {
        launch(requireNotNull(token))
        rule.onNodeWithText("Rzecznik").performClick()
        waitForText("Model językowy nie jest włączony")
        waitForText("Właściciel materiału nie widzi treści tej rozmowy")
        rule.onNodeWithText("Pytanie").performTextInput("Kiedy podpisano umowę najmu?")
        rule.onNodeWithText("Zapytaj").performClick()
        waitForText("cytat z zapisanego tekstu")
        waitForText("Umowa najmu została podpisana piątego marca")
        rule.onAllNodesWithText("p1").onFirst().performClick()
        waitForText("transkrypcja nagrania")
        waitForText("SHA-256 materiału")
        rule.onAllNodesWithText(hidden, substring = true).assertCountEquals(0)
    }

    @Test fun aRefusedTokenClearsTheAccountAndSaysSo() {
        launch("nieprawidlowy.token")
        rule.onNodeWithText("Rzecznik").performClick()
        waitForText("Token jest nieprawidłowy albo został cofnięty")
        rule.onAllNodesWithText("Umowa najmu", substring = true).assertCountEquals(0)
    }
}
