# AGEDS — niezależny odbiór fundamentów

2026-09-30. Zakres: integralność i migracja, importy komunikacji, kolejka i runy,
serwerowy skan, API, cytaty segmentowe, pakiet metadanych oraz protokół koordynacji.

**W sprawdzonym zakresie serwerowym nie pozostał materialny bloker.** Android
przeszedł przegląd kodu; kompilacja i odbiór na urządzeniu wymagają osobnej
weryfikacji. Wszystkie dane wykonanych testów były syntetyczne. Nie wykonywano
realnej inferencji ASR ani odczytu prywatnego korpusu użytkownika.

## Wyniki

| Sprawdzenie | Wynik | Co rzeczywiście potwierdza |
|---|---|---|
| Zbiorcze `python -m pytest server/tests -q` | **123 testy i 84 subtesty przeszły**, 3 ostrzeżenia deprecacji | Integrację bieżącego backendu, w tym TestClient i procesy CLI. |
| Niezależna ścieżka API | **31/31 sprawdzeń przeszło** | Upload → akwizycje → kolejka → syntetyczny worker → wersja tekstu → adnotacja/cytat → eksport i walidacja. |
| Migracja starego schematu | Poprawne i uszkodzone historyczne metadane zachowane; ponowna migracja idempotentna | Stare rekordy nie są zastępowane fikcyjnym pochodzeniem. |
| Integralność pozyskania | Dwa źródła zachowane; granice spraw sprawdzone; uszkodzona kopia odrzucona | Deduplikacja nie usuwa kolejnej akwizycji i nie naprawia po cichu zapisanego materiału. |
| Niezależna walidacja pakietu | Zmienione digesty, brakujące relacje i sfabrykowany cytat wykrywane | Graf metadanych i selektor są sprawdzane; bajty źródeł i prawdziwość wypowiedzi nie są weryfikowane. |
| CLI verify z rzeczywistym pakietem zawierającym cytat | Poprawna walidacja bez utworzenia katalogów lub plików w pustym cwd | Weryfikacja selektora nie uruchamia konfiguracji storage ani pracy jobów. |
| Przegląd dokumentów koordynacji | Bez deklaracji dodatkowych slotów, doręczenia przez historię lub aktywności w tle | Protokół wymaga odczytanego commitu, stabilnego ID i potwierdzenia odbioru. |

Ostrzeżenia dotyczą dotychczasowego `on_event` FastAPI i aliasu AnyIO używanego
przez TestClient. Nie były błędami wykonania. Testy po kolejnych zmianach powinny
być uruchamiane ponownie w zakresie tych zmian; powyższa liczba jest wynikiem
konkretnego punktu odbioru, nie stałą właściwością repo.

## Kontrprzykłady znalezione i poprawione

- Po osiągnięciu limitu obserwacji skanera kolejny timestamp nazwy mógł nadpisać
  wcześniejszą obserwację. Zapis dotyczy teraz wyłącznie utworzonego rekordu.
- Sam limit plików nie ograniczał drzewa pustych katalogów. Dodano globalny
  limit odczytywanych wpisów i jawny wynik niepełnego pokrycia.
- Zbiorcze enqueue zerowało istniejące ręczne priorytety. Brak nowego priorytetu
  zachowuje dotychczasowy; jawne zero nadal zmienia go.
- Nieograniczony NaN w odnowieniu lease mógł dać pusty expiry. Czasy i okresy
  lease są teraz sprawdzane jako skończone i mieszczące się w kontrakcie.
- Źródło zdarzenia mogło należeć do innej sprawy niż artefakt. Zgodność znanej
  sprawy jest sprawdzana przed zapisem.
- Dawny `stored_path` mógł wskazywać symlink, mimo ochrony nowego miejsca zapisu.
  Weryfikacja obejmuje również ten historyczny path oraz rozmiar i hash kopii.
- Metadata stat sprzed capture mogły wyglądać jak dokładne dane przechwyconego
  streamu. Zachowano stat przed/po i jawny zakres niepewności snapshotu.
- Upload Androida przekazywał samą nazwę. API zachowuje dokładny lokator URI
  oraz klientową ścieżkę jako niezweryfikowaną metadaną źródłową.
- Dowolny historyczny failed job przesłaniał późniejszy sukces w stanie listy.
  Stan końcowy odnosi się do właściwego aktualnego zadania.
- Bardzo duży integer czasu lub ID mógł wywołać błąd SQLite/konwersji zamiast
  odrzucenia wejścia. Dodano granice typu, czasu i identyfikatorów.
- JSON `1e999` omijał sprawdzanie tokenów NaN/Infinity. Metadata upload odrzucają
  również nieskończoność powstałą przy konwersji wykładniczej.
- Weryfikacja cytatu importowała moduł DB i mogła utworzyć storage w cwd.
  Czysta projekcja selektora nie ma już tego skutku ubocznego.

## Sprawdzone niezmienniki

Hash opisuje przechwycone bajty; nie dowodzi autorstwa, prawdy ani daty powstania.
Nowa akwizycja jest dopisywana również przy ponownym użyciu artefaktu. Sprzeczne
etykiety źródeł pozostają dostępne. Raw materiał, zdarzenie parsera, tekst ASR,
adnotacja i cytat nie zastępują się wzajemnie.

Idempotencja importu dotyczy wystąpienia w źródle, dzięki czemu dwa identyczne SMS
pozostają dwoma rekordami. AM/PM, północ i południe są rozróżniane. Nieznana strefa,
niejednoznaczna kolejność dat i nieznane stulecie nie otrzymują pozornej pewności.

Token, status, expiry i run chronią publikację workera. Wynik, indeks FTS, audit
i zakończenie zadania powstają w jednej transakcji. Spóźniony worker nie publikuje
ani nie oznacza nowej próby jako failed. Kolejne udane runy dają kolejne wersje.
`language_probability` pozostaje prawdopodobieństwem identyfikacji języka.

Skaner nie zapisuje, kopiuje ani wysyła źródeł. Kolizje nazw zachowują osobne
ścieżki. Formuły nie są wykonywane. Limity, uszkodzenia, symlinki i pominięcia są
widoczne w pokryciu. Wartości z nazw, nagłówków i korelacji pozostają kandydatami.
Pole skanera i obserwacja akwizycji w SQLite zachowują odrębne semantyki.

Cytat wskazuje konkretny transcript ID i sąsiednie segmenty, zachowuje dokładne
znaki oraz sprawdzalny selektor i hash. Późniejszy tekst nie przepina starego
cytatu. Jego precision jest segmentowe; odsłuch audio nie został wykonany.

## Granice odbioru i dalszy zakres

- Stare zdarzenia z `external_id=NULL` nie zostały retrospektywnie deduplikowane.
  Nowa interpretacja parsera obecnie daje jawny konflikt; współistnienie wersji
  interpretacji zdarzeń potrzebuje osobnego kontraktu.
- Przed upgrade trzeba zatrzymać stare workery. Już załadowany stary kod może
  omijać nowe fencing. Lease korzysta z zegara systemowego; odporność na dowolny
  skok tego zegara nie została wykazana.
- SQLite append-only triggers i prawa plików są kontrolami aplikacji. WORM,
  podpis i pełny chain of custody pozostają osobnymi zdolnościami.
- Pakiet jest **metadata-only i unsigned**. Nie zawiera mediów, nie wznawia
  zadań i nie gwarantuje dokładnego restore/replay. Walidator sprawdza digesty,
  referencje i selektory, a nie pełną prawdziwość źródeł lub wszystkie znaczenia
  domenowych pól.
- Nowy skaner CSV/XLS/XLSX działa na serwerze. Android nadal korzysta z seed JSON
  i indeksowania nagrań; samodzielny skan SAF bez seed pozostaje kolejnym zadaniem.
- Test adaptera syntetycznego nie potwierdza jakości faster-whisper. Build Android,
  test telefonu, dokładne selektory słów i realne integracje ekosystemu wymagają
  odrębnych dowodów wykonania.

Rutynowe strategie techniczne w powierzonym zakresie pozostają autonomiczne.
Reguły koordynacji nie przywracają obowiązku zatwierdzania każdej większej,
odwracalnej decyzji przez użytkownika.
