# Android — odbiór fali 6

Build assembleDebug, testDebugUnitTest i desktopTest przeszedł w izolowanej
kopii źródeł. Wykonano 57 zadań Gradle, 132 testy Android JVM i 75 testów
desktop JVM: łącznie 207, bez błędów i pominięć. Sprawdzono 65 hashy wejść
i brak zmian źródeł podczas buildu. Weryfikacja podpisu APK zakończyła się
kodem 0.

SHA-256 APK: `2274ccf3b3db1949d5fe8e2829a69e34c451738d91fcc9b5cc2825766bd32d14`.

Nowe przypadki sprawdzają 4 MiB zakodowanych danych historii, koszt realnych
serializerów, Unicode, granice, reset oraz spóźnione odpowiedzi. To limit
payloadu kolekcji, nie limit pamięci JVM, transportu ani pełnej transkrypcji.
Telefon i runtime SAF nadal nie były dostępne. Nie wykonano nowej próby
instrumentation. Dokładne wejścia i wyniki: `ANDROID_WAVE6_BUILD_RECEIPT.json`.
