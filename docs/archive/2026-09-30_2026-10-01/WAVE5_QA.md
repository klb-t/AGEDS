# Odbiór fali 5

Pełny backend: 293 testy i 427 podprzypadków, bez błędów; trzy znane
ostrzeżenia deprecacji. Android: 184 JVM, 57 zadań Gradle, 62 hashe wejść.
Node: 34 testy. Chromium: 20 przypadków, brak błędów strony.

125 wersji, 125 cytatów i 125 adnotacji w syntetycznej bazie przeszło
trzystronicowy odczyt 50→100→125 w rzeczywistej przeglądarce. Spóźniona
strona nie zmieniła wybranej wersji; niezgodna granica ID niczego nie dopisała.
Zachowano dokładne cytaty, weryfikację pakietu oraz rzeczywiste odtwarzanie WAV.

Indeksowane zapytania filtrują artefakt i rodzaj, ograniczają wiersze/bajty
oraz pracę SQLite. Starsze API listowe zachowuje zgodność; nowe klienty
korzystają ze stron. Granica ID wyklucza zwykłe późniejsze dopisania, lecz
nie zamraża edycji/usunięć/backfilli między żądaniami. To nie trwały snapshot DB.

Podglądy HTML nietranskrypcyjnych tekstów i zdarzeń mają jawne ograniczenia
liczby i długości. Pełny tekst wybranej transkrypcji jest odrębnym odczytem.
Limit 1000 pozycji klienta nie ogranicza sumy bajtów tak skutecznie jak
odrębny budżet payloadu — zadanie kolejnej fali. Brak telefonu/SAF runtime,
nowego ASR i zewnętrznej integracji ekosystemu.

Dowody: `WAVE5_PAGES_QA.md`, `WAVE5_BROWSER_RECEIPT.json`,
`WAVE5_ANDROID_QA.md`, `ANDROID_WAVE5_BUILD_RECEIPT.json`.
