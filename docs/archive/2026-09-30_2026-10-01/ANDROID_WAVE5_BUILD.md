# Android — odbiór fali 5

Izolowany build 2026-10-01 00:16–00:20 UTC: 57 zadań Gradle,
184 testy JVM (125 Android, 59 desktop), bez błędów i pominięć.
62 hashe wejść zgodne po buildzie. Receipt: `ANDROID_WAVE5_BUILD_RECEIPT.json`.

Nowe listy wersji, cytatów i adnotacji korzystają ze stron, mają odrębne
kursory, retry, jawne pokrycie i limit 1000 pozycji. Dociąganie starszych
wersji nie przepina cytatu; spóźnione odpowiedzi po zmianie serwera/materiału
są odrzucane. Brak nowego endpointu nie uruchamia nieograniczonego fallbacku.
7 testów autora, 22 niezależne testy kontraktu i 10 testów cyklu stron
włączono do pełnego builda; zachowano wcześniejsze 7 testów workspace.

APK debug: 14140437 bajtów, SHA-256
`2a56f2f49c3761c3f78b07cbed2c7ce465d7f18eb2293845a3e71721cc8c34ba`.
Weryfikacja podpisu apksigner: kod 0.

Nie wykonano telefonu, SAF runtime ani odsłuchu. Limit liczby pozycji
nie jest limitem całej pamięci klienta; agregat bajtów zachowanych stron
został wskazany jako kolejny konkretny zakres.
