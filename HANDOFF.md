# AGEDS — przekazanie do rozwoju i osobnego wątku zarządzania

Stan bieżący: nocny przyrost 2026-10-01. Fundament PR #2 scalono w `main`
w commicie `b6b6a4e4a1fdafb53447bceaeab904aa7b660ee2`.
PR #3 scalono w main (`8c3efb9e65999dd4931b22b555329da76379c31e`).
Bieżący opublikowany checkpoint: fala 5. branch `codex/ageds-night-20261001`, PR #4 (otwarty).
Kod fali 5 opublikowany i sprawdzony; nie wykonano scalenia PR #4 do main.
Trwa fala 6: zweryfikowany strumień wejścia ASR i agregatowe limity danych
zachowywanych przez klienty. Szczegóły przydziałów w indeksie.
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
| Android | Wszystkie URI kolizyjnych nazw, wybór konkretnego kandydata, prywatny manifest wyboru, jawne wysłanie na wybrany serwer, wyniki/błędy każdego pliku, wersja adnotacji. Widok Źródła skanuje SAF bez seed: CSV/TSV, XLSX, podstawowy WAV i inventory, raw komórki, lokatory, kolizje, ograniczenia. Dawny katalog JSON jest opcjonalny. Natywny XLS: ograniczona, jawnie częściowa projekcja BIFF8. UTF-16 BOM i hipotezy separatorów CSV z metadanymi niejednoznaczności. Aktualny build i testy: `docs/ANDROID_WAVE2_BUILD.md`; telefon pozostaje oddzielnym odbiorem. |

`scanner.observations` to obserwacje pól i hipotez; SQLite `source_observations`
to pozyskania bajtów. Wspólny zapis JSON nie oznacza wspólnej semantyki.

## Odbiór

Fundament i fala 1 zachowują swoje historyczne receipty. Fala 2: **183 testy
backend i 207 podtestów**, 14 testów Node oraz **11 testów rzeczywistego Chromium**.
Odbiór parserów: 34 bezpośrednie testy JVM, w tym 20 niezależnych testów
adwersarialnych. Końcowy zintegrowany build i dokładne hashe zapisują
`docs/ANDROID_WAVE2_BUILD.md` oraz `docs/ANDROID_WAVE2_BUILD_RECEIPT.json`.

Wykonano rzeczywistą lokalną inferencję tiny.en CPU/int8 na syntetycznej mowie,
z zapisem 17 słów, cytatem i dokładnym roundtripem inertnego archiwum.
Naprawiono dwie niezgodności zależności i granicę typów NumPy/JSON.
Raw ASR zawiera błąd rozpoznania — nie skorygowano go ani nie zadeklarowano
jakości na ludzkim korpusie. Dowód: `docs/REAL_ASR_SMOKE_RECEIPT.json`.
Chromium sprawdził także odtwarzanie zapisanego cytatu starszej wersji podczas
wybrania nowszej; receipt: `docs/BROWSER_NIGHT_RECEIPT.json`.

Sześć testów dostawcy SAF i osobna aplikacja testowa skompilowały się, ale
nie wykonały: brak urządzenia/emulatora. `docs/ANDROID_SAF_PROVIDER_ACCEPTANCE.md`
odróżnia tę kompilację od testu runtime i podaje procedurę kontynuacji.

Przed aktualizacją działającej instalacji zatrzymaj stare workery i wykonaj
backup bazy oraz store. Ograniczenia append-only SQLite i uprawnienia plików
nie są fizycznym WORM ani kryptograficznym podpisem łańcucha pochodzenia.

## Następny zakres wykonawczy

1. Konkretny kontrakt wymiany dowodów: pakiet wskazujący wersję, selektor,
   pochodzenie i jawne braki oraz niezależny konsument-fixture w AGEDS.
   Nie otwierać lokatorów źródła i nie deklarować adaptera partnera bez testu
   z rzeczywistym drugim projektem. Praca tej nocy dotyczy tylko repo AGEDS.
2. Uruchomić gotowe sześć testów SAF, gdy będzie dostępny runtime Android;
   oddzielnie sprawdzić picker, cykl uprawnień i wybór nagrań. Brak emulatora
   w obecnym środowisku jest obserwowaną blokadą, nie dowodem błędu aplikacji.
3. Odpowiednik wyboru cytatu wersji i zakresu w kliencie Android — uzgodnić
   istniejący kontrakt HTTP, a następnie przetestować konkretne zachowanie.
4. Rozszerzać XLS/teksty tylko dla konkretnego nieobsługiwanego wzorca:
   aktualne granice opisują `docs/NATIVE_XLS.md` i `docs/NATIVE_SOURCE_FORMATS.md`.
5. Po odzyskaniu miejsca sprawdzić czystą instalację przypiętych zależności ASR;
   bieżący poprawiony zestaw jest przetestowany wykonaniem, druga świeża
   instalacja nie została wykonana. Jakość/Polski ASR to osobny eksperyment.
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

## Odbiór fali 3

221 testów backend + 272 podprzypadki, 116 JVM, 13 Chromium: przeszły.
Eksport cytatu i niezależny konsument zachowują wersję, surowy tekst i pochodzenie;
lokatory pozostają inertne. Android wybiera wersję, cytat i zakres odtwarzania.
Build: `docs/ANDROID_WAVE3_BUILD.md`. Brak testu telefonu/SAF runtime.
Kontrakt pakietu nie dowodzi prawdy ani wdrożonej integracji partnera.
Po checkpointcie przejęto następne rozłączne zadania; nie zakończono aktywnej pracy.

## Odbiór fali 4

267 backend +354 podprzypadki, 145 JVM, 23 Node i 17 Chromium przeszło.
Naprawiono race podmiany pliku audio i wyciek połączenia inicjalizacji SQLite.
XLS obsługuje ograniczone kontynuacje SST; wyszukiwanie i identyfikatory JS
odmawiają nieobsługiwanych danych jawnie. `docs/WAVE4_QA.md` zachowuje także
opis pierwszego nieudanego testu i konkretnej poprawki. Telefon nadal niebadany.

## Odbiór fali 5

293 backend +427 podprzypadków,184 JVM,34 Node,20 Chromium przeszło.
Stronicowane wersje/cytaty/adnotacje mają jawne pokrycie i granice ID;
Chromium sprawdził po125 wpisów i wybór wersji podczas opóźnionego odczytu.
Szczegóły i granice: `docs/WAVE5_QA.md`. Limit1000 pozycji nie zastępuje
agregatowego budżetu danych klienta — to następny aktywny zakres.
