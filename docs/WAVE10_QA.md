# Fala 10 — ograniczona weryfikacja inertnego archiwum

Pełny backend: **417 testów i 523 podtesty przeszły**, 3 istniejące ostrzeżenia
deprecation, zero błędów. Polecenie: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<cached-deps>:$PWD python -m pytest server/tests -q`.
Nowy zakres ma 47 testów: 9 budżetu, 7 integracji archiwum, 10 niezależnych
granic, 10 dokładnego roundtrip oraz 11 powtarzanej pracy projekcji.

Poprzednia implementacja przyjęła mały pakiet z ukrytym JSON głębszym niż
max_depth i publikowała archiwum. Dekodowała też tę samą wersję raz na cytat.
Naprawa wprowadza wspólny budżet struktury outer + interpretowanych pól,
wstępne liczenie przed dekodowaniem, lokalny cache sukcesów i błędów wersji
oraz konserwatywny budżet pracy projekcji. Przekroczenie jest trwałym błędem
bieżącej weryfikacji; nie uruchamia dalszych kosztownych projekcji.

Węzły to wartości i kontenery, bez kluczy. Root ma głębokość 0, rozwinięty
segments_json/selector_json zaczyna na 4. Opaque pola i nieprzypięte wersje
nie stają się automatycznie interpretowanym JSON. Surowe łańcuchy pozostają
niezmienione. Budżet wizyt obejmuje dotknięte segmenty/słowa i długości tekstu;
nie jest dokładnym pomiarem CPU, czasu ani heap. Osobne fazy importu/odczytu
mają świeży budżet. Duplikaty kluczy dają ograniczony komunikat diagnostyczny.

Rzeczywisty CLI wykonał 9 podprocesów: poprawny pakiet 3243 B → inertny SQLite
16384 B → dokładnie ten sam kanoniczny pakiet. Ukryta głębokość w danych
cytatu została odrzucona; nie powstały częściowe wyniki ani katalogi docelowe.
Głęboki nieprzezroczysty raw metadata pozostał zachowany. Istniejącego celu
nie nadpisano; wejścia syntetyczne i ich mtime/hashy pozostały niezmienione.
Sześć fingerprintów kodu z receiptu zgadza się z aktualnymi źródłami.

Dodatkowa kontrola koordynatora: 1000 deterministycznych wygenerowanych
poprawnych JSON (seed 61001, Unicode/escaping/wcięcia) dało zgodne liczby
węzłów w preflight i strukturze. To kontrola pomocnicza, nie pełny dowód parsera.

Dowody: ARCHIVE_VERIFICATION_BUDGET.md, ARCHIVE_VERIFICATION_BUDGETS.md,
WAVE10_ARCHIVE_SEMANTIC_QA.md, WAVE10_ARCHIVE_ROUNDTRIP_QA.md,
WAVE10_PROJECTION_WORK_QA.md oraz ARCHIVE_BUDGET_CLI_SMOKE_RECEIPT.json.

APK, przeglądarka i ASR nie zmieniły się i nie były ponownie testowane.
Nie wykonano telefonu ani integracji partnera. Archiwum jest inertne:
bez live restore, replay, wznowienia jobs, odczytu lokatorów czy mediów.
Następny konkretny krok: atomowo uporządkować anulowanie skanu i publikację
prywatnego cache Androida, z deterministycznym testem wyścigu.
