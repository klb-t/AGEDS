package dev.klbt.ageds

import dev.klbt.ageds.core.ScannedSourceFile

/** Presentation only: missing older metadata never becomes a new verification claim. */
data class SourceAudioDisplay(val summary: String, val details: List<String>)

object SourceAudioProjection {
    private const val DURATION_BASIS = "declared_data_bytes_divided_by_header_byte_rate"

    fun display(file: ScannedSourceFile): SourceAudioDisplay? {
        val observation = file.wavHeader
        val extension = file.name.substringAfterLast('.', "").lowercase()
        val wav = extension in setOf("wav", "wave") || file.mime?.lowercase() in setOf("audio/wav", "audio/wave", "audio/x-wav", "audio/x-pn-wav")
        if (observation == null && !wav) return null
        val hash = file.sha256?.let { "Zapisany SHA-256 całego pliku: $it" }
            ?: "SHA-256 całego pliku: nie obliczono; obserwacja nagłówka go nie zastępuje."
        if (observation == null) {
            val duration = file.audioDurationSec?.takeIf { it.isFinite() && it >= 0 }
            return SourceAudioDisplay("WAV: brak zapisanej obserwacji nagłówka; podstawa czasu i zakres odczytu nieznane.", buildList {
                add("Ten wpis nie zawiera obserwacji nagłówka WAV. Nowy skan może uzupełnić podstawę i zakres odczytu.")
                duration?.let { add("Zapisany czas (podstawa nieznana): $it s. Nie zapisano podstawy tego wyliczenia.") }
                add("Liczba przejrzanych bajtów i zakres sprawdzenia danych audio: nieznane.")
                add(hash)
            })
        }
        val label = when (observation.status) {
            "observed" -> "Odczytano podstawowe pola nagłówka WAV"
            "partial" -> "Częściowy odczyt nagłówka WAV"
            "malformed" -> "Niepoprawny nagłówek WAV"
            "unsupported" -> "Nieobsługiwana odmiana WAV"
            "size_mismatch" -> "Sprzeczne deklaracje rozmiaru WAV"
            else -> "Nieznany stan obserwacji WAV"
        }
        val duration = observation.declaredDurationSec?.takeIf {
            it.isFinite() && it >= 0 && observation.durationBasis == DURATION_BASIS &&
                observation.status in setOf("observed", "partial")
        }
        val bodySummary = if (observation.bodyValidated) "zakres sprawdzenia danych audio niepotwierdzony w tym widoku" else "dane audio niesprawdzone"
        return SourceAudioDisplay("$label; $bodySummary.", buildList {
            if (duration != null) {
                add("Czas z deklaracji nagłówka: $duration s — zadeklarowany rozmiar danych podzielony przez liczbę bajtów na sekundę z nagłówka.")
            } else add("Nie ustalono czasu na podstawie obsługiwanej, spójnej deklaracji nagłówka.")
            add("To nie jest czas zmierzony podczas odtwarzania ani potwierdzenie, że nagranie da się odtworzyć.")
            if (observation.bytesInspected >= 0) add("Przejrzano ${observation.bytesInspected} B z początku pliku.")
            else add("Liczba przejrzanych bajtów jest nieznana: zapis zawiera nieprawidłowy licznik.")
            if (observation.endOfInput) add("Odczyt dotarł do końca pliku; nie oznacza to sprawdzenia próbek audio.")
            else add("Odczyt nagłówka nie objął potwierdzonego końca pliku. Dalsza zawartość pozostaje poza tym odczytem.")
            if (!observation.bodyValidated) add("Zawartość danych audio nie została sprawdzona; nagłówek nie potwierdza ich kompletności ani poprawności.")
            else add("Zapis zgłasza sprawdzenie danych audio, ale ten widok nie potwierdza jego zakresu ani odsłuchu.")
            observation.providerSizeBytes?.let { add("Rozmiar podany przez źródło: $it B.") }
            observation.riffDeclaredBytes?.let { add("Całkowity rozmiar zadeklarowany w nagłówku RIFF: $it B.") }
            add(hash)
        })
    }
}
