# AGEDS — przekazanie do rozwoju i osobnego wątku zarządzania

Stan: przyrost fundamentu z 2026-09-30, na bazie zdalnego commitu
`99389ba2ba1a5873b4183b592c62f7aa666276c0`.
Branch pracy: `codex/ageds-foundation-20260930`.
Potwierdzenie publikacji i aktualne zadania: `coordination/state.json`.

## Mandat

Użytkownik powierzył autonomiczne prowadzenie rozwoju AGEDS zgodnie ze swoją
filozofią i w kontekście ekosystemu. Rutynowe wybory i odwracalne wdrożenia
prowadzi koordynator. Osobny wątek zarządzania został wskazany przez użytkownika,
ale nie jest tu utworzony ani zweryfikowany. Historia rozmów pomaga odszukać
wymagania; wymiana pracy opiera się na odczytanych commitach i identyfikatorach
zadań, decyzji i wyników. Nie ma automatycznej pracy ani komunikacji po zamknięciu
aktywnego wykonania.

W tej sesji działają koordynator i maksymalnie sześciu agentów równocześnie.
Role zarządzania i wykonania mogą rotować. Limit innego wątku należy sprawdzić
w tamtym wątku; nie zakładamy dodatkowej puli sześciu procesów.

## Wdrożony zakres

| Obszar | Zachowanie i granica |
|---|---|
| Zachowanie źródeł | Hash tego samego przechwyconego strumienia; kompletny plik publikowany bez nadpisania istniejących bajtów. Każde pozyskanie ma obserwację pochodzenia. Hash nie dowodzi autorstwa ani prawdy. |
| Migracja | Addytywna i transakcyjna. Stare metadane, także uszkodzone, zachowane; brakująca historia pozyskania jawnie nieznana. Ten sam hash/lokator w różnych sprawach obecnie jest odrzucany, wymaga przyszłego kontraktu tożsamości. |
| Importy | Idempotencja nowych importów SMS/WhatsApp według pozycji wystąpienia, nie samego tekstu. AM/PM działa; strefa, dwucyfrowy rok i niejednoznaczna kolejność dat pozostają jawne. Starych zdarzeń bez ID nie deduplikujemy wstecznie. |
| Kolejka | Atomowy claim, heartbeat, odzyskanie wygasłej lease, token własności. Spóźniony worker nie publikuje. Każda próba ma run; każdy sukces nowy transcript. Lease zakłada prawidłowy zegar systemowy. |
| Wyniki i cytaty | Adnotacja wskazuje wyświetloną wersję. Cytat segmentowy zachowuje dokładny tekst, hash i selektor. Weryfikacja oznacza zgodność z zapisaną wersją ASR; nie oznacza odsłuchu lub prawdziwości wypowiedzi. |
| Skaner | Read-only CSV/TSV, XLS, XLSX, WAV, inwentaryzacja/hash innych plików. Kolizje nazw, kandydaci powiązań i sprzeczności zachowane. Limity, brak adaptera i uszkodzenia jawne. Safe no-follow descriptors wymagają wspieranego systemu POSIX. |
| Pakiet | Snapshot 16 tabel metadanych, digests i walidacja grafu oraz cytatów. Bez bajtów źródeł, podpisu, wznowienia jobs, replay lub przywrócenia live DB. |
| Android | Wszystkie URI kolizyjnych nazw, wybór konkretnego kandydata, prywatny manifest wyboru, jawne wysłanie na wybrany serwer, wyniki/błędy każdego pliku, wersja adnotacji. Corpus nadal wymaga seed JSON. Build i test telefonu są oddzielnym odbiorem. |

`scanner.observations` to obserwacje pól i hipotez; SQLite `source_observations`
to pozyskania bajtów. Wspólny zapis JSON nie oznacza wspólnej semantyki.

## Odbiór

Wykonano syntetyczne testy jednostkowe, współbieżności i HTTP; dokładny wynik
pełnego przebiegu zapisuje `docs/FOUNDATION_QA.md` oraz `coordination/state.json`.
Niezależny odbiór obejmuje migrację starego schematu, powtórne importy,
przerwanie workera, przypięcie starszej wersji, ingerencję w pakiet i skan bez
modyfikacji źródeł. Nie przeprowadzono realnej inferencji ASR ani testu telefonu.

Przed aktualizacją działającej instalacji zatrzymaj stare workery i wykonaj
backup bazy oraz store. Ograniczenia append-only SQLite i uprawnienia plików
nie są fizycznym WORM ani kryptograficznym podpisem łańcucha pochodzenia.

## Następny zakres wykonawczy

1. Rzeczywisty build Android i próba telefonu; naprawa narzędzi/builda na
   podstawie błędów, bez deklarowania APK na podstawie przeglądu kodu.
2. Natywny skan wskazanego drzewa SAF bez seed: raw rekordy/komórki, lokatory,
   konflikty i pokrycie. Zachować wspólne kontrakty, bez kopiowania serwerowej
   semantyki ścieżek do Android URI.
3. Cytaty słowne oraz kontrolowany odtwarzacz wskazanego przedziału; dotychczas
   API udostępnia zweryfikowane bajty pliku i cytaty segmentowe.
4. Dokładny eksport/reimport metadanych z zachowaniem tożsamości i wersji;
   osobno zakres mediów oraz wymagania podpisu. Import nie wznawia starych lease.
5. Kolejne zdolności pięciu warstw AGEDS i rozumowania LLM, wdrażane na konkretnych
   przykładach. RAW odpowiedź modelu i deterministyczna polityka liczb/wyliczeń
   pozostają osobnym profilem; ogólny brainstorm nie zmienia jego wymagań.

Kierunki współpracy z ChatADHD, iOmatrix, Loom/LEM, WatchDog i PixelSpace są
koncepcyjne. Używać `ECOSYSTEM.md` i `docs/ARCHITECTURE_RULES.md`; adapterów nie
deklarować jako działających przed testem z rzeczywistym drugim projektem.

## Start osobnego wątku zarządzania

Przeczytaj ten plik, `AGENTS.md`, `coordination/state.json`,
`coordination/README.md`, `docs/ARCHITECTURE_RULES.md` i `ECOSYSTEM.md` na tej samej
rewizji repo. Potwierdź odczytany commit i task ID. Prowadź priorytety i decyzje,
przekazując wykonaniu zlecenia z rozłącznym zakresem i kryteriami odbioru.
Przyjmuj wyniki dopiero z ich dowodami testów i ograniczeniami. Nie wymagaj
codziennych zatwierdzeń użytkownika dla pracy już objętej jego mandatem.
