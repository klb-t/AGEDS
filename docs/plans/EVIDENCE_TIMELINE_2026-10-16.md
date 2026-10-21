# Evidence Timeline & Analysis — Etap 0 i plan (2026-10-16)

Zlecenie: prompt „AGEDS — Evidence Timeline & Analysis” (kompilacja 2026-10-16,
wygenerowany automatycznie z historii rozmów; użytkownik zaznaczył, że może zawierać
błędy i że pierwszeństwo mają: zdrowy rozsądek, filozofia kodowania, dokumentacja repo).
Baza: `main` @ `9c1d513`. Gałąź robocza: `ccr-d25c3cb3-qgbhql`.

## 1. Weryfikacja stanu z promptu (A4) wobec kodu

Bazowe testy na `9c1d513` w tej sesji: backend 429 + 588 subtestów, Node 85, `verify_repository.py` OK.

| Twierdzenie A4 | Stan w kodzie | Rozbieżność / skutek |
|---|---|---|
| Content store po SHA-256, „identyczne bajty → jeden artefakt” | Plik w store jest jeden, ale `artifacts` ma `UNIQUE(sha256, source_locator)`: te same bajty spod innego lokatora tworzą **osobny wiersz artefaktu** (wspólny plik w store). | Deduplikacja treści w katalogu musi grupować po `sha256` ponad wierszami `artifacts`; nie zmieniamy istniejącego klucza (migracja niszcząca). |
| `links`, `tags` | Tabele istnieją; brak API/UI do nich. | Rozszerzamy addytywnie. |
| `/api/timeline` = prosta lista | Potwierdzone. Sortuje po `COALESCE(ts_start, created_at)` — zdarzenie bez czasu dostaje **czas importu**; SMS zapisuje UTC `+00:00`, WhatsApp lokalny czas bez strefy, a sortowanie porównuje te napisy leksykalnie. | Projekcja osi czasu musi rozróżniać typ czasu i status strefy; nowe API zachowuje stary kształt odpowiedzi. |
| ASR: język raz na plik | Potwierdzone: `model.transcribe(audio, word_timestamps=True, vad_filter=True)` bez `language`; receptura nie jest przypięta do zadania (`jobs.payload_json` pusty). | Nowa strategia jako osobny adapter/run; receptura przypięta do zadania (zbieżne z briefem B-AG-01 z audytu ekosystemu). |
| Skaner CSV/TSV/XLS/XLSX/WAV + inventory | Potwierdzone; brak rekurencji w archiwa i sniffingu sygnatur. | Etap 1. |
| Eksport `manifest.json` / archiwum metadanych | Stała lista tabel `packages.TABLES`. | Nowe tabele rozszerzeń **nie** trafiają do pakietu, dopóki eksport nie zadeklaruje ich jawnie (zadanie F). |
| Brak autoryzacji API | Potwierdzone. | Zadanie G (token lokalny dla kontraktu odczytu). |

## 2. Narzędzia pokrewne

| Narzędzie | Stan | Decyzja |
|---|---|---|
| ChatADHD (`klb-t/chatadhd`) | Dostępne w sesji. Python/Kivy + `loom/` (C++20, C ABI `loom.h`, REST/SSE `loom/server`, CLI). Audyt ekosystemu (2026-10-11) ma ustalenia EA-AGEDS-001…012 i briefy B-AG-01…03 (receptura ASR przypięta do zadania, słowniki parsera jako dane, konfiguracja adresowalna) oraz most „M1 AGEDS → ChatADHD”. | Adapter ChatADHD i eksport do Loom projektujemy wobec tych interfejsów (zadanie G); briefy B-AG-01/02 realizujemy tam, gdzie pokrywają się z zakresem (D, B). |
| Loom (`klb-t/loom`) | Osobne repo istnieje (ostatni push 2026-10-03); nowszy kod Loom jest w `chatadhd/loom/`. | Czytamy `chatadhd/loom` jako bieżący. |
| `chatarchive`, UCAF 0.1, `select_takeout.py`, `package_takeout.py` | Brak w AGEDS i ChatADHD (pełny grep) i brak repozytorium o tej nazwie wśród 7 dostępnych. | **`unknown`**. Wzorzec profile → explain → promote i sidecary `*.annotations.ndjson` implementujemy jako własny, udokumentowany format AGEDS z polem `format_compat: unknown`; adapter UCAF pozostaje do napisania po otrzymaniu specyfikacji. |

## 3. Środowisko tej sesji (granice dowodów)

- Hugging Face zablokowany przez proxy (403) → **brak realnej inferencji Whisper**; strategie ASR testowane atrapą modelu. PyPI dostępne.
- Brak Tesseract, espeak/TTS, 7z/unrar; jest `ffmpeg` i Chromium (Playwright).
- Brak urządzenia Android. Android nie jest zmieniany w tym przyroście.

## 4. Konflikty promptu z niezmiennikami repo

- Prompt prosi o „deduplikację: identyczne bajty → jeden artefakt”. Repo ma tożsamość artefaktu per (hash, lokator). Wygrywa niezmiennik zachowania pochodzeń bez przepisywania schematu: grupa treści (hash) jest projekcją katalogu.
- Prompt mówi o „podpisanym manifeście”/custody — repo wprost nie deklaruje podpisu. Raporty dostają hashe i sekcję metody, **bez** deklaracji podpisu.
- Prompt Etapu 3 sugeruje „detekcję Whispera na oknie segmentu”; prawdopodobieństwo języka nie jest pewnością transkrypcji (AGENTS.md) — zapisujemy je z tą etykietą.

## 5. Plan zadań (ID, zależności, właściciel plików)

Fundament F0 (koordynator) wprowadza punkty rozszerzeń, aby tory miały rozłączne pliki:
`server/app/schema/<obszar>.py` (zakresy wersji migracji), `server/app/api/<obszar>.py`
(routery + nawigacja), `server/app/commands/<obszar>.py` (CLI), `server/tests/corpus/<część>.py`
(korpus syntetyczny), `server/app/timeinfo.py` + `event_times` (kandydaci czasu),
`collections` + `source_observations.collection_id`.

| ID | Etap | Zakres | Zależy od |
|---|---|---|---|
| AGEDS-TASK-20261016-F0 | 0 | fundament, plan, ledger | — |
| AGEDS-TASK-20261016-A | 1 | katalog: kolekcje, archiwa rekurencyjne, sygnatury, deduplikacja z pochodzeniami, coverage i luki, opis (profile→explain→promote), CLI + widok + eksport CSV/XLSX/HTML | F0 |
| AGEDS-TASK-20261016-B | 2 | importery: ChatGPT (drzewo, shardy), Claude.ai (mapowanie w danych), MBOX/EML, ICS, VCF, log połączeń, nazwy nagrań (regexy w danych), Takeout My Activity, media WhatsApp, EXIF; deduplikacja logiczna między eksportami | F0 |
| AGEDS-TASK-20261016-C | 2 | projekcja osi czasu z regułą wyboru czasu (wersjonowana), korelacje jako odwoływalne hipotezy, rozszerzone `/api/timeline`, widok swimlane z wirtualizacją | F0, B (kontrakt `event_times`) |
| AGEDS-TASK-20261016-D | 3 | receptura ASR przypięta do zadania, strategia per-segment LID (PL/NL/EN), porównanie runów, WER/CER, adaptery OCR i diaryzacji, szacunek czasu kolejki | F0 |
| AGEDS-TASK-20261016-E | 4–5 | kategorie (prywatna konfiguracja), etykietowanie z pochodzeniem, sesje, model przerw (dolna/prawdopodobna/górna), świece, wrażliwość, przebiegi cech; prerejestracja hipotez, dossier, event study z testem permutacyjnym | C |
| AGEDS-TASK-20261016-F | 6 | raporty (HTML główny, PDF z HTML, XLSX/CSV, JSON), sekcja metody, manifest raportu, pakiety wielu cytatów, jawne tabele rozszerzeń w eksporcie | A–E |
| AGEDS-TASK-20261016-G | 7 | kontrakt API odczytu + token lokalny, adapter ChatADHD (stub, tryb cytatów vs interpretacyjny), eksport grafu dla Loom z kwarantanną hipotez | F0 |
| AGEDS-TASK-20261016-H | 7 | udogodnienia: FTS po warstwach, zapisane wyszukiwania, tagi hurtowe, teczki, zakładki, skróty, motyw jasny/ciemny, i18n PL/NL/EN; propozycje „wymaksowania” zgodne z niezmiennikami | F0 |
| AGEDS-TASK-20261016-P | — | `verify_repository.py`: heurystyki prywatności (telefony, e-maile spoza domen przykładowych, ścieżki katalogów domowych) | F0 |

Domyślne decyzje (odwracalne; warunek zmiany): SQLite + addytywne migracje (zmiana przy
mierzonej potrzebie współbieżności); zależności opcjonalne (`py7zr`, `rarfile`, `tesseract`,
`pyannote`) za adapterami z jawnym `unavailable`; LLM wyłącznie jako opt-in per run
z zapisem providera — domyślnie brak wywołań.
