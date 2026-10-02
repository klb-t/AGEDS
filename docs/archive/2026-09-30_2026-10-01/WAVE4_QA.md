# Odbiór fali 4

Końcowy pełny backend: **267 testów + 354 podprzypadki**, bez błędów;
3 istniejące ostrzeżenia deprecacji. Android: **145 JVM**, 58 hashy wejść,
57 zadań Gradle, APK i podpis sprawdzone. Rzeczywisty Chromium: **17 przypadków**,
bez błędów strony. Node: 9 testów ID oraz 14 wcześniejszych testów zakresów.

Najważniejsza naprawa: przed zmianą deterministyczna podmiana ścieżki między
hashowaniem a FileResponse pozwalała otrzymać inne bajty przy HTTP 200.
Teraz jeden deskryptor pozostaje przypięty, rodzice ścieżki otwierani są
bez podążania za symlinkami, a każdy bufor jest weryfikowany przed wysłaniem.
Zmiana po rozpoczęciu odpowiedzi przerywa transmisję — nie może już zmienić
wysłanego statusu HTTP. Jawny limit profilu: 4 GiB. Brak kopii mediów.

Wyszukiwanie zachowuje składnię FTS i dane źródłowe, jawnie odrzuca błędne
zapytania i przekroczenia budżetu; pokazuje niepełną listę wyników.
Przeglądarka odrzuca ID, których nie umie dokładnie reprezentować.
XLS rozszerzono o sprawdzone przypadki SST CONTINUE; pokrycie nadal partial.

W pierwszym pełnym przebiegu 1 test porównania pliku SQLite nie przeszedł
(264 pozostałe przeszły): baseline obejmował inicjalizację serwera, a nie tylko
operację eksportu. Zidentyfikowano także otwarte połączenie init_db, ponieważ
kontekst sqlite3 zarządza transakcją, lecz nie zamyka połączenia. Dodano
explicit close i test sukcesu/błędu migracji; test eksportu pobiera baseline
po startupie. Niezależny test producenta tylko do odczytu zachowano.
Końcowy pełny przebieg opisany wyżej przeszedł.

Receipty: `ANDROID_WAVE4_BUILD_RECEIPT.json`, `WAVE4_BROWSER_RECEIPT.json`.
Niezależny odbiór: `WAVE4_MEDIA_QA.md`, `WAVE4_XLS_QA.md`.
Nie wykonano telefonu, SAF runtime, nowego ASR ani integracji z innym projektem.
