package dev.klbt.ageds.core

import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals

class CorpusPrioritySelectionTest {
    @Test fun legacySeedHasNoImplicitCaseSelection() {
        val seed = Json.decodeFromString<CorpusSeed>(
            """{"presets":[{"id":"review_group","label":"Review group"}]}"""
        )
        assertEquals(emptyList(), seed.priorityPresetIds())
    }

    @Test fun privateDefaultsSelectOnlyAvailableUniqueIds() {
        val seed = CorpusSeed(
            presets = listOf(CorpusPreset("a", "A"), CorpusPreset("b", "B")),
            defaultPresetIds = listOf("b", "unknown", "b", "a")
        )
        assertEquals(listOf("b", "a"), seed.priorityPresetIds())
    }

    @Test fun changingSeedCannotRetainAnUnavailableDefault() {
        val previous = CorpusSeed(presets = listOf(CorpusPreset("a", "A")), defaultPresetIds = listOf("a"))
        assertEquals(listOf("a"), previous.priorityPresetIds())
        assertEquals(emptyList(), previous.copy(presets = emptyList()).priorityPresetIds())
    }
}
