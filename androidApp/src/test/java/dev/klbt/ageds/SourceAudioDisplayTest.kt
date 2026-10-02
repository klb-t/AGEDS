package dev.klbt.ageds

import dev.klbt.ageds.core.ScannedSourceFile
import dev.klbt.ageds.core.WavHeaderObservation
import org.junit.Assert.*
import org.junit.Test

class SourceAudioDisplayTest {
    private fun file(observation: WavHeaderObservation? = null, duration: Double? = null, hash: String? = null) =
        ScannedSourceFile("content://synthetic/audio", "sample.wav", "sample.wav", kind = "audio",
            sha256 = hash, audioDurationSec = duration, wavHeader = observation)
    private fun header() = WavHeaderObservation(status = "observed", bytesInspected = 44, endOfInput = false,
        declaredDurationSec = 120.0, durationBasis = "declared_data_bytes_divided_by_header_byte_rate")
    private fun text(file: ScannedSourceFile) = SourceAudioProjection.display(file)!!.let { it.summary + "\n" + it.details.joinToString("\n") }

    @Test fun headerDurationIsExplicitDeclarationNotPlaybackAndDoesNotUseLegacyValue() {
        val shown = text(file(header(), duration = 999.0))
        assertTrue(shown.contains("Czas z deklaracji nagłówka: 120.0 s"))
        assertTrue(shown.contains("nie jest czas zmierzony podczas odtwarzania"))
        assertFalse(shown.contains("999.0"))
    }

    @Test fun boundedPrefixDisclosesInspectedBytesUnreadRemainderAndNoFullHash() {
        val shown = text(file(header()))
        assertTrue(shown.contains("Przejrzano 44 B"))
        assertTrue(shown.contains("Dalsza zawartość pozostaje poza tym odczytem"))
        assertTrue(shown.contains("Zawartość danych audio nie została sprawdzona"))
        assertTrue(shown.contains("SHA-256 całego pliku: nie obliczono"))
    }

    @Test fun endOfInputAndIndependentFullHashDoNotClaimAudioValidation() {
        val shown = text(file(header().copy(endOfInput = true), hash = "synthetic-whole-file-hash"))
        assertTrue(shown.contains("Odczyt dotarł do końca pliku"))
        assertTrue(shown.contains("nie oznacza to sprawdzenia próbek audio"))
        assertTrue(shown.contains("Zapisany SHA-256 całego pliku: synthetic-whole-file-hash"))
        assertFalse(shown.contains("SHA-256 całego pliku: nie obliczono"))
    }

    @Test fun olderCachedDurationHasUnknownBasisAndUnknownReadCoverage() {
        val shown = text(file(duration = 7.5))
        assertTrue(shown.contains("podstawa czasu i zakres odczytu nieznane"))
        assertTrue(shown.contains("Zapisany czas (podstawa nieznana): 7.5 s"))
        assertTrue(shown.contains("Nie zapisano podstawy"))
        assertFalse(shown.contains("Czas z deklaracji nagłówka"))
        assertFalse(shown.contains("Przejrzano 0 B"))
    }


    @Test fun freshUnopenedWavDoesNotInventAnEarlierScan() {
        val shown = text(file())
        assertTrue(shown.contains("brak zapisanej obserwacji nagłówka"))
        assertFalse(shown.contains("wcześniejszy", ignoreCase = true))
        assertFalse(shown.contains("wcześniej", ignoreCase = true))
        assertFalse(shown.contains("Zapisany czas"))
    }

    @Test fun invalidOrUnsupportedBasisNeverFallsBackToLegacyDuration() {
        val observations = listOf(
            header().copy(durationBasis = "unknown"), header().copy(durationBasis = null),
            header().copy(declaredDurationSec = Double.NaN), header().copy(declaredDurationSec = Double.POSITIVE_INFINITY),
            header().copy(declaredDurationSec = -1.0), header().copy(status = "malformed"),
            header().copy(status = "unsupported"), header().copy(status = "size_mismatch"), header().copy(status = "future_status"),
        )
        observations.forEach { observation ->
            val shown = text(file(observation, duration = 999.0))
            assertTrue(shown.contains("Nie ustalono czasu"))
            assertFalse(shown.contains("Czas z deklaracji nagłówka"))
            assertFalse(shown.contains("999.0"))
        }
    }

    @Test fun otherFormatsRemainOutsideWavProjection() {
        for (name in listOf("table.csv", "sheet.xlsx", "recording.mp3")) {
            assertNull(SourceAudioProjection.display(ScannedSourceFile("content://synthetic/item", name, name)))
        }
        assertNotNull(SourceAudioProjection.display(ScannedSourceFile("content://synthetic/item", "unnamed", "unnamed", mime = "audio/wav")))
    }

    @Test fun inconsistentSizeStatusIsVisibleWithoutDurationAndNegativeCountersAreUnknown() {
        val shown = text(file(header().copy(status = "size_mismatch", bytesInspected = -1,
            providerSizeBytes = 1000, riffDeclaredBytes = 2000)))
        assertTrue(shown.contains("Sprzeczne deklaracje rozmiaru WAV"))
        assertTrue(shown.contains("nieprawidłowy licznik"))
        assertTrue(shown.contains("źródło: 1000 B"))
        assertTrue(shown.contains("RIFF: 2000 B"))
        assertFalse(shown.contains("Przejrzano -1"))
    }
}
