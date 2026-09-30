# AGEDS — odbiór fali 2

Wykonano sześć rozłącznych zadań agentowych oraz integrację koordynatora.
Kod opublikowano małymi commitami `[skip ci]` na `codex/ageds-night-20261001`.
PR #4 pozostaje otwarty; publikacja gałęzi nie oznacza wdrożenia działającego serwera.

## Wynik

| Obszar | Dowód | Granica |
|---|---|---|
| Backend | 183 testy + 207 podtestów, zero błędów | Dane syntetyczne; 3 wcześniejsze ostrzeżenia deprecacji |
| Android/core | 64 + 10 testów JVM; 57 rzeczywiście wykonanych zadań; 45 hashy wejść | Brak telefonu/runtime SAF |
| Niezależny przegląd parserów | 20 testów, 5 znalezionych i naprawionych usterek | Wchodzą w powyższe 74; nie dodawać ich ponownie |
| Chromium | 11 rzeczywistych testów, native HTMLAudioElement, zero page errors | Brak pomiaru akustycznego alignmentu/opóźnienia wyjścia |
| Node | 14 testów odtwarzacza na atrapie audio | Oddzielne od realnego Chromium |
| ASR | Produkcyjny worker, tiny.en CPU/int8, 17 słów; cytat i inertny roundtrip passed | Jeden syntetyczny głos angielski; błąd rozpoznania zachowany |
| SAF test APK | 6 testów skompilowanych, 60 zadań Gradle | 0 testów wykonanych; brak urządzenia/emulatora |

Parser XLS zawsze deklaruje partial; nie obiecuje pełnej obsługi historycznych
skoroszytów. CSV przechowuje podstawę doboru separatora i niejednoznaczność,
a kodowanie domyślne nie staje się pewnością co do intencji źródła.
Formuły/macra nie są wykonywane; prywatnego korpusu nie używano.

Realny ASR wykrył brak `requests`, niezgodność PyAV 19 oraz różnicę między
NumPy scalar a liczbowymi typami JSON przy ocenie znaczników słów. Poprawki
przeszły rzeczywistą inferencję oraz niezależny przegląd. Ponownej, całkowicie
czystej instalacji zależności nie wykonano z powodu braku miejsca.

W Chromium sprawdzono także zapisany cytat starszej wersji podczas wybrania
nowszej, granicę odtwarzania, anulowanie, spóźnione odpowiedzi i dosłowne
wyświetlanie HTML ze źródła. Nie potwierdza to prawdy cytatu ani jego alignmentu.

## Pochodzenie i punkt wznowienia

Zaakceptowany kod: `903d3d3004ccf878b91d407b4d4983660b2081b5`,
drzewo `8b5761237e6fb64c934d3efea1f3adb1166005b8`. Dokumenty odbioru są
publikowane późniejszym commitem, który nie zmienia budowanych wejść.
Przed każdą publikacją porównano remote HEAD, po niej sprawdzono SHA drzewa;
wszystkie 45 wejść APK porównano też z treścią commitowaną przez Git.

Szczegóły: `ANDROID_WAVE2_BUILD_RECEIPT.json`, `N15_ADVERSARIAL_QA.md`,
`REAL_ASR_SMOKE_RECEIPT.json`, `BROWSER_NIGHT_RECEIPT.json`,
`ANDROID_SAF_PROVIDER_RECEIPT.json`. Historyczne receipty pozostają zachowane.

Następny konkretny krok: N14 — ograniczony kontrakt wymiany dowodów z wersją,
selektorem i pochodzeniem oraz niezależnym konsumentem-fixture wewnątrz AGEDS.
Bez automatycznego odczytu lokatorów i bez deklarowania gotowej integracji
innych projektów. Claim fali 2 jest zakończony; świeże wznowienie musi odczytać
zdalny stan zgodnie z `coordination/NIGHT_WORK.md`.
