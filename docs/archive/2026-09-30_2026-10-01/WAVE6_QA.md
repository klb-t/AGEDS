# Odbiór fali 6

321 testów backend i 430 podtestów przeszło; trzy istniejące ostrzeżenia
zależności. 207 testów JVM (132 Android,75 desktop),49 Node i21 rzeczywistego
Chromium przeszło. Build wykonał57 zadań, potwierdził65 hashy wejść, brak
zmian źródeł podczas kompilacji i poprawny podpis APK.

Niezależny test odtworzył lukę starego ASR: podmiana pliku tylko podczas
odczytu modelu przechodziła oba hashe ścieżki i publikowała niewłaściwy tekst.
Worker przekazuje teraz jeden zweryfikowany czytnik; każdy zwracany bufor
sprawdza hashe bloków, a końcowa kontrola tego samego deskryptora poprzedza
publikację chronioną lease. Błąd odczytu pozostaje nieodwracalnym błędem tego
czytnika także po złapaniu wyjątku przez adapter i przywróceniu pliku.
Nie twierdzimy, że system plików jest niemodyfikowalny lub model godny zaufania.

Rzeczywisty lokalny tiny.en CPU/int8 odczytał ten obiekt przez PyAV:10 odczytów,
jedna końcowa weryfikacja i zamknięty deskryptor.17 słów, dokładny cytat oraz
kanoniczny roundtrip inertnego archiwum przeszły. Syntetyczna angielska mowa
nie jest oceną jakości na korpusie, polskiego ASR, odsłuchu ani alignmentu.

Android i przeglądarka zatrzymują historię przy1000 wpisów lub4MiB sumy
serializowanych wierszy UTF-8 na kolekcję. Zachowują prefiks bez pomijania
niepasujących wierszy i pokazują niepełne pokrycie. To nie jest limit heap,
DOM, transportu ani pojedynczej pełnej transkrypcji. Chromium potwierdził
zatrzymanie przy4 z6 cytatów po około900kB każdy.

Pierwsza próba nowego scenariusza Chromium miała błędne oczekiwanie testu:
przycisk jest wyłączony także podczas pobierania, więc odczytano przejściowy
napis „Wczytuję”. Poprawiono synchronizację na końcowy komunikat o budżecie;
produkt nie wymagał zmiany. Pierwszy receipt zachowano, ponowny komplet21
scenariuszy przeszedł z hashami zgodnymi z końcowymi plikami.

Program30 ról w `coordination/AGENT_PROGRAM.md` mapuje odpowiedzialności na
sześć rotujących slotów; nie deklaruje dodatkowej puli ani uruchomienia
osobnego wątku zarządzania. Telefon/SAF runtime pozostaje niewykonany.
Nie testowano integracji z innym repozytorium, nie zmieniano korpusu.

Dowody: `WAVE6_ASR_QA.md`, `VERIFIED_READER.md`,
`VERIFIED_ASR_SMOKE_RECEIPT.json`, `WAVE6_CLIENT_BUDGET_QA.md`,
`WAVE6_BROWSER_RECEIPT.json`, `WAVE6_BROWSER_INITIAL_RECEIPT.json`,
`ANDROID_WAVE6_BUILD_RECEIPT.json`.

Następny konkretny krok: ograniczony odczyt nagłówka dużego WAV w natywnym
skanie, z jawnym pochodzeniem deklarowanego czasu i brakiem pełnego hasha.
