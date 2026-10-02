# Fala 11 — anulowanie i publikacja cache skanu

**357 testów JVM przeszło**: 246 Android i 111 desktop, 57 zadań Gradle,
88 hashy wejść niezmienione. APK i podpis zweryfikowano. Pierwszy pełny build
tej fali zakończył się sukcesem. Dodano 20 testów bramki, cache, wyścigu i
rzeczywistego przepływu skanu do typowanego odczytu metadanych.

Wcześniej ensureActive i finalny Files.move były oddzielnymi operacjami.
Niezależny test deterministycznie odtworzył publikację spóźnionego wyniku.
Nowa per-ViewModel bramka używa tokenów tożsamości i wspólnej blokady dla
unieważnienia oraz finalnego atomowego move. Kodowanie, staging, fsync i
sprawdzenia anulowania pozostają poza tą blokadą. Stary interfejs write
zachowuje zgodność; runtime skanu używa writeGuarded.

Testy z barierami sprawdziły obie kolejności: unieważnienie pierwsze zachowuje
poprzednie bajty; move pierwsze może prawidłowo zakończyć publikację przed
powrotem anulowania. Nowa generacja odrzuca starszy token. Awaria move zwalnia
blokadę i usuwa plik tymczasowy, a świeża generacja może zapisać następny wynik.
Nie ma obietnicy cofania już zakończonego zapisu ani koordynacji wielu procesów
lub niezależnych ViewModeli.

Źródła pozostają tylko do odczytu. Testy typowanego cache wykonały prawdziwy
silnik CSV/WAV na syntetycznych plikach; odrzucenie i awarie zachowały prior
cache oraz wejścia. Integrację CorpusVm start/cancel/onCleared niezależnie
przejrzano i skompilowano. To nie jest wykonanie lifecycle na telefonie.

Dowody: ANDROID_WAVE11_BUILD_RECEIPT.json, ANDROID_WAVE11_BUILD.md,
SOURCE_SCAN_PUBLICATION_GATE.md, SOURCE_SCAN_CACHE_PUBLICATION.md,
ANDROID_SCAN_PUBLICATION_INTEGRATION.md, WAVE11_PUBLICATION_RACE_QA.md,
WAVE11_GUARDED_CACHE_QA.md i WAVE11_LIFECYCLE_QA.md.

Backend, przeglądarka i ASR nie zmieniły się w tej fali; zachowują wcześniejsze
wyniki, bez deklaracji ponownego wykonania. Bez GitHub Actions, pobierania
modeli i usług płatnych. Następne wykryte luki dotyczą surowych rekordów CSV
(Unicode separator mylony z fizyczną linią) i tekstu XLSX (fonetyczne adnotacje
mieszane z bazową treścią komórki). Pierwsza ma małą reprodukcję serwera,
druga wymaga wykonania proponowanego fixture przed poprawką.

Dodatkowo ponownie skompilowano sześć testów instrumentation z aktualnym
kodem: assembleDebugAndroidTest offline, 26 zadań wykonanych i 34 aktualne.
Pierwsza próba odmówiła pobierania brakujących zależności; powtórka użyła
już istniejącego cache dostawcy. Zero pobrań. SyntheticDocumentsProvider nie
trafił do manifestu aplikacji debug. Runtime testów nadal **0**. Dowód:
`ANDROID_WAVE11_SAF_COMPILE_RECEIPT.json`.
