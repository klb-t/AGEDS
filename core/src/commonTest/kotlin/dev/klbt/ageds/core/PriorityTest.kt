package dev.klbt.ageds.core

import kotlin.test.Test
import kotlin.test.assertTrue

class PriorityTest {
    @Test fun humanPriorityDominatesHeuristics() {
        val human = TranscriptionPriority.score(PrioritySignals(manualPriority = 10))
        val heuristic = TranscriptionPriority.score(PrioritySignals(durationSeconds = 3600, taggedLegal = true, hasConflict = true))
        assertTrue(human > heuristic)
    }
}

