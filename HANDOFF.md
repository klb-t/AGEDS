# AGEDS — przekazanie do rozwoju i osobnego wątku zarządzania

Stan bieżący: nocny przyrost 2026-10-01. Fundament PR #2 scalono w `main`
w commicie `b6b6a4e4a1fdafb53447bceaeab904aa7b660ee2`.
Branch pracy: `codex/ageds-night-20261001`, PR #3.
**Najświeższy odbiór i kolejka: `coordination/night-20261001.json`.**
Procedura wznowienia: `coordination/NIGHT_WORK.md`.
`coordination/state.json` zachowuje odbiór fundamentu i odsyła do nowego etapu.

## Mandat

Użytkownik powierzył autonomiczne prowadzenie rozwoju AGEDS zgodnie ze swoją
filozofią i w kontekście ekosystemu. Rutynowe wybory i odwracalne wdrożenia
prowadzi koordynator. Osobny wątek zarządzania został wskazany przez użytkownika,
ale nie jest tu utworzony ani zweryfikowany. Historia rozmów pomaga odszukać
wymagania; wymiana pracy opiera się na odczytanych commitach i identyfikatorach
zadań, decyzji i wyników. Aktywne wykonanie nie jest bezterminowym procesem. Na wyraźne nocne zlecenie
utworzono sześć zaplanowanych wznowień około 01:00–06:00 Europe/Amsterdam
2026-10-01. Każde musi odczytać stan i respektować aktywny claim, aby nie
dublować pracy. Utworzenie harmonogramu nie potwierdza wykonania jego
przyszłych przebiegów. Osobnego wątku zarządzania nadal nie zweryfikowano.

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
| Wyniki i cytaty | Adnotacja wskazuje wyświetloną wersję. Cytat segmentowy lub słowny zachowuje dokładny tekst, hash i selektor. Wybór słów wymaga zgodnych zapisanych znaczników ASR; brak precyzji pozostaje jawny. Weryfikacja oznacza zgodność z zapisaną wersją ASR; nie oznacza odsłuchu lub prawdziwości wypowiedzi. |
| Skaner | Read-only CSV/TSV, XLS, XLSX, WAV, inwentaryzacja/hash innych plików. Kolizje nazw, kandydaci powiązań i sprzeczności zachowane. Limity, brak adaptera i uszkodzenia jawne. Safe no-follow descriptors wymagają wspieranego systemu POSIX. |
| Pakiet | Snapshot 16 tabel metadanych, digests i walidacja grafu oraz cytatów. Dokładny kanoniczny roundtrip do inertnego archiwum SQLite i z powrotem. Bez bajtów źródeł, podpisu, wznowienia jobs, replay lub przywrócenia live DB. |
| Android | Wszystkie URI kolizyjnych nazw, wybór konkretnego kandydata, prywatny manifest wyboru, jawne wysłanie na wybrany serwer, wyniki/błędy każdego pliku, wersja adnotacji. Widok Źródła skanuje SAF bez seed: CSV/TSV, XLSX, podstawowy WAV i inventory, raw komórki, lokatory, kolizje, ograniczenia. Dawny katalog JSON jest opcjonalny. Natywny XLS pozostaje unsupported. Build debug APK i 26 testów JVM przeszły; telefon pozostaje oddzielnym odbiorem. |

`scanner.observations` to obserwacje pól i hipotez; SQLite `source_observations`
to pozyskania bajtów. Wspólny zapis JSON nie oznacza wspólnej semantyki.

## Odbiór

Fundament miał 126 testów i 84 podtesty. Nocny przyrost rozszerza ten odbiór;
aktualne dokładne liczby oraz dowody zapisują `docs/NIGHT_QA.md`
i `coordination/night-20261001.json`. Rzeczywisty build Android wykonał
57 zadań oraz 16 testów Android JVM i 10 desktop. Sprawdzono podpis APK
i 35 hashy źródeł; receipt: `docs/ANDROID_NIGHT_BUILD_RECEIPT.json`.

Odbiór obejmuje cytat starszej wersji, raw błędne dane, dokładny roundtrip
archiwum, odrzucenie niejednoznacznego JSON i ochronę źródeł. Node sprawdza
odtwarzacz na atrapie audio. Przegląd wykrył i naprawił wykonywalny HTML
w snippetach wyszukiwania; fragment jest teraz wyświetlany jako tekst.
Nie przeprowadzono realnej inferencji ASR ani interakcji SAF na telefonie.

Przed aktualizacją działającej instalacji zatrzymaj stare workery i wykonaj
backup bazy oraz store. Ograniczenia append-only SQLite i uprawnienia plików
nie są fizycznym WORM ani kryptograficznym podpisem łańcucha pochodzenia.

## Następny zakres wykonawczy

1. Test dostawcy SAF na urządzeniu/emulatorze: folder bez seed, kolizje URI,
   odmowa uprawnień, anulowanie, źródła bez zmian i selekcja nagrań. APK gotowy.
2. Natywny adapter XLS oraz dodatkowe kodowania/separatory, po sprawdzeniu
   ograniczeń adaptera. Unsupported musi pozostać jawne do realnego odbioru.
3. Rzeczywista inferencja ASR na małym syntetycznym nagraniu; oddzielić
   wykonanie adaptera od jakości na prywatnym korpusie.
4. Cytaty słowne i przeglądarkowy wybór/odtwarzanie zakresu są wdrożone.
   Następnie odbiór interaktywny i ewentualny analogiczny klient Android.
5. Inertny roundtrip metadanych jest wdrożony. Kontrolowany restore live DB,
   zakres mediów i podpis wymagają osobnego kontraktu; starych lease nie wznawiać.
6. Profile pięciu warstw AGEDS i rozumowanie LLM — konkretne przypadki,
   jawne RAW odpowiedzi oraz deterministyczna polityka liczb/wyliczeń.

Kierunki współpracy z ChatADHD, iOmatrix, Loom/LEM, WatchDog i PixelSpace są
koncepcyjne. Używać `ECOSYSTEM.md` i `docs/ARCHITECTURE_RULES.md`; adapterów nie
deklarować jako działających przed testem z rzeczywistym drugim projektem.

## Start osobnego wątku zarządzania

Przeczytaj ten plik, `AGENTS.md`, `coordination/night-20261001.json`,
`coordination/NIGHT_WORK.md`, `coordination/state.json`,
`coordination/README.md`, `docs/ARCHITECTURE_RULES.md` i `ECOSYSTEM.md` na tej samej
rewizji repo. Potwierdź odczytany commit i task ID. Prowadź priorytety i decyzje,
przekazując wykonaniu zlecenia z rozłącznym zakresem i kryteriami odbioru.
Przyjmuj wyniki dopiero z ich dowodami testów i ograniczeniami. Nie wymagaj
codziennych zatwierdzeń użytkownika dla pracy już objętej jego mandatem.
