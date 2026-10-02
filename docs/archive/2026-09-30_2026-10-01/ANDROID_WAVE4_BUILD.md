# Android — odbiór fali 4

Izolowany build 2026-09-30 23:59–2026-10-01 00:00 UTC zakończony powodzeniem.
145 testów JVM (115 Android, 30 desktop), bez błędów i pominięć; 57 zadań Gradle.
58 hashy wejść zgodnych ze źródłami po buildzie. Receipt:
`ANDROID_WAVE4_BUILD_RECEIPT.json`.

Nowy zakres to ograniczone SST CONTINUE w BIFF8: teksty przechodzące między
rekordami i zmiany kodowania znaków. Surowe rekordy pozostają zachowane;
nieprawidłowa tablica nie publikuje częściowo zdekodowanych tekstów.
Kontynuacje dodatkowych danych rich-text/phonetic pozostają jawnie unsupported.
12 testów autora i 17 niezależnych weszło do pełnego builda.

APK debug: 14124053 bajty; SHA-256
`2fbb0a5996ef5ccf873b9ec769cdc6e36b1cdfd77a8f0db02671489d9c459076`.
Podpis sprawdzony apksigner (kod 0). Nie testowano telefonu, emulatora ani SAF
runtime. Testy parsera korzystają z trudnych danych syntetycznych.
