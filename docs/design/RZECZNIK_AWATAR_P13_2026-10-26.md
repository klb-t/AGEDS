# Rzecznik — awatar (P13), projekt bez implementacji (2026-10-26)

Status: **[PROPONOWANE]**, nic z tego dokumentu nie jest zaimplementowane. Wymaganie: R98/R99 właściciela
(„konwersacyjny interfejs — tekst, głos, później awatar Unreal”, streszczenie w
`chatadhd/docs/architecture/AGEDS_RZECZNIK_GPT_2026-10-25.md`). „Unreal” jest w wymaganiu przykładem kierunku, nie
rozstrzygnięciem; wybór technologii i stylu to decyzja właściciela (pkt 8). Stan wejściowy: P10 (tekst) i P12 (głos:
STT za zgodą mówiącego, TTS odpowiedzi przez tę samą bramkę) w `docs/design/RZECZNIK_MODEL_2026-10-25.md`.

## 1. Zasada: awatar jest warstwą prezentacji, nie nowym kanałem treści

- Awatar **nie dostaje żadnej treści**, której nie dostaje już odczyt głosowy P12: jedyne wejście to dźwięk odpowiedzi
  (i ewentualnie jej tekst do synchronizacji ust), wytworzony po `ContentGate`, weryfikatorze cytatów i zgodzie
  dostawcy. Nie ma osobnego promptu, osobnej pamięci ani osobnego dostępu do bazy.
- Awatar **nie zastępuje etykiet i cytatów**. Twierdzenia z etykietą słowną i cytatami są widoczne obok, zawsze;
  awatar nie może mówić niczego, czego nie ma w zapisanej odpowiedzi (mówi dokładnie tekst twierdzeń).
- Awatar **nie podnosi wiarygodności**. Twarz i głos zwiększają zaufanie odbiorcy niezależnie od jakości podstawy;
  dla materiału dowodowego to ryzyko, nie zaleta. Stąd: jawna informacja „syntetyczna postać, odczytuje odpowiedź
  wygenerowaną z udostępnionego materiału”, brak ekspresji sugerującej pewność (kiwanie przy `proven`, uśmiech), brak
  podobieństwa do realnej osoby (właściciela, świadka, adwokata).
- Każdy element jest wyłączalny przez odbiorcę (preferencja per konto), a `prefers-reduced-motion` wyłącza animację.

## 2. Warianty

| | A. Bez awatara | B. Wskaźnik mówienia | C. Stylizowany awatar 3D w przeglądarce | D. Unreal Engine + MetaHuman (Pixel Streaming) |
|---|---|---|---|---|
| Co to jest | dzisiejszy stan P10/P12 | abstrakcyjna forma (fala, okrąg) poruszana amplitudą dźwięku | postać 3D (GLB) renderowana lokalnie w WebGL (three.js), usta z dźwięku/czasów słów | fotorealistyczna postać renderowana na serwerze GPU, wideo strumieniowane do przeglądarki |
| Gdzie liczy się obraz | — | urządzenie odbiorcy | urządzenie odbiorcy (telefon/laptop) | serwer z GPU (chmura) |
| Koszt stały | 0 | 0 | 0 (poza pracą nad modelem postaci) | GPU w chmurze: rząd kilku USD/h na instancję (jeden dostawca podaje ok. 8,20 USD/h za RTX 4090 w promocji — [iRender, 2026](https://irendering.net/unreal-engine-cloud-rendering/); niezweryfikowane, do wyceny) |
| Koszt zmienny | TTS jak P12 | TTS jak P12 | TTS jak P12 | TTS + godziny GPU + transfer wideo |
| Zgodność z budżetem 1 €/dzień | tak | tak | tak | **nie** przy pracy ciągłej; najwyżej sesje na żądanie, minuty, po zmianie budżetu przez właściciela |
| Opóźnienie | — | ≈ odtwarzanie dźwięku | ≈ odtwarzanie dźwięku | + start instancji (minuty przy zimnym starcie) + opóźnienie strumienia |
| Dolina niesamowitości | brak | brak | niska przy stylizacji (nie‑fotorealizm) | **wysoka**: fotorealizm + niedokładne usta po polsku = najbardziej niepokojący wynik |
| Licencja | — | — | three.js (MIT); biblioteka lip-sync np. TalkingHead — licencję sprawdzić w repozytorium przed użyciem | MetaHuman w ramach licencji Unreal Engine; dla UE 5.6+ obowiązuje EULA Unreal, darmowo poniżej 1 mln USD przychodu wg [metahuman.com/license](https://www.metahuman.com/en-US/license) (warunki do potwierdzenia u Epic) |
| Prywatność | jak P12 | jak P12 | jak P12 (render lokalny, brak nowych danych do chmury) | wideo powstaje na serwerze; serwer GPU widzi tekst/dźwięk odpowiedzi → wymaga zgody dostawcy jak Vertex (pkt 5) |
| Praca | 0 | mała | średnia (postać, polski lip-sync, testy na telefonie) | duża (projekt UE, serwer, orkiestracja, bezpieczeństwo strumienia) |

### 2.1 Synchronizacja ust — źródła sygnału (dla C i D)

1. **Amplituda dźwięku** (Web Audio `AnalyserNode`): niezależna od języka, zero dodatkowych danych, ruch ust
   „mówi/nie mówi”, bez kształtów głosek. Najmniej realistyczna, najmniej ryzykowna; dobra dla B i stylizowanego C.
2. **Czasy słów z Google TTS**: API `text:synthesize` w `v1beta1` zwraca `timepoints` tylko dla znaczników
   `<mark>` w SSML (`enableTimePointing: [SSML_MARK]`) — [dokumentacja](https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1beta1/text/synthesize).
   Wstawiając `<mark>` przed każdym słowem dostajemy początek słowa; **wizemów Google TTS nie zwraca** (nie znalazłem
   ich w dokumentacji). Kształty ust trzeba wtedy wyliczyć z tekstu.
3. **Wizemy z tekstu (reguły dla polskiego)**: ortografia polska jest dość regularna (dwuznaki sz/cz/rz/ch/dz, ą/ę),
   więc moduł reguł grafem→wizem jest wykonalny. Biblioteka TalkingHead (three.js) ma wbudowany lip-sync dla
   angielskiego, fińskiego i litewskiego (beta) i pozwala dodać moduł językowy — [repozytorium](https://github.com/met4citizen/TalkingHead)
   (stan i licencję sprawdzić w repozytorium przed użyciem). Polski moduł = nasza praca + test z native speakerem.
4. **Wizemy od dostawcy TTS** (np. zdarzenia wizemów u innych dostawców): oznacza nowego dostawcę, nowej zgody (R100
   dotyczy Vertex/Google) — poza zakresem bez decyzji właściciela.

## 3. Rekomendacja (preset, odwracalny)

**C w stylizacji nie‑fotorealistycznej, z B jako trybem lekkim i A jako domyślnym**:
`avatar.mode = none` domyślnie; odbiorca może włączyć `indicator` albo `stylized_3d`. Lip-sync etapami:
amplituda (od razu) → czasy słów z `<mark>` → polskie reguły wizemów (po teście). D (Unreal/MetaHuman) zostaje
**opcją na wyraźną decyzję właściciela** ze zmianą budżetu — nie mieści się w 1 €/dzień przy pracy ciągłej i ma
najwyższe ryzyko doliny niesamowitości przy polskiej mowie.

Uzasadnienie: (1) brak nowego kanału danych i nowego dostawcy; (2) koszt ≈ 0 ponad P12; (3) działa na telefonie
adwokata bez instalacji; (4) stylizacja obniża ryzyko, że postać będzie odbierana jako „człowiek, który wie”.

## 4. Ustawienia (dane, nie reguły; każda wartość testowana jak w P9)

| Klucz | Wartości (otwarte) | Preset | Zakres |
|---|---|---|---|
| `avatar.mode` | `none`, `indicator`, `stylized_3d`, `unreal_stream` | `none` | instalacja; odbiorca może tylko zawęzić |
| `avatar.model_asset` | identyfikator pliku GLB w `static/` (bez zewnętrznych CDN postaci) | brak | instalacja |
| `avatar.lip_sync` | `amplitude`, `word_timepoints`, `visemes_pl_rules` | `amplitude` | instalacja |
| `avatar.expressions` | `neutral_only`, `idle_motion` | `neutral_only` (bez ekspresji zależnej od statusu twierdzenia) | instalacja |
| `avatar.disclosure_text` | tekst | „Syntetyczna postać odczytuje odpowiedź wygenerowaną z udostępnionego materiału. Sprawdź cytaty.” | instalacja |
| `avatar.recipient_opt_out` | `true`/`false` | `true` (odbiorca zawsze może wyłączyć) | odbiorca |
| `avatar.unreal.max_minutes_per_day` | liczba | `0` (wyłączone) | instalacja; ×budżet z `advocate.daily_budget_eur` |

Zmiana `avatar.mode` na szerszy wariant (`unreal_stream`) przechodzi przez `set_setting(..., confirm_widening)`
z P9, bo wysyła tekst odpowiedzi do kolejnego serwera.

## 5. Bezpieczeństwo i zgody

- C: render lokalny; serwer wysyła tylko to, co P12 (MP3 odpowiedzi) i ewentualnie tablicę czasów słów — bez treści
  spoza odpowiedzi. Test kanarków jak S5: żaden bajt z materiału spoza `basis` nie trafia do odpowiedzi awatara.
- D: serwer GPU dostaje tekst/dźwięk odpowiedzi i renderuje wideo → **nowy odbiorca danych**: wymaga zgody dostawcy
  (`provider_consents`) na zakres, jak Vertex; strumień wideo musi być uwierzytelniony tym samym tokenem i nie może
  być cache'owany przez proxy; nagrania wideo nie są zapisywane (ustawienie, preset `false`).
- Postać nie może być wzorowana na realnej osobie bez jej zgody; preset: postać generyczna, nazwana „Rzecznik”.

## 6. Plan testów

S16 awatar nie otwiera nowej ścieżki: wszystkie dane wejściowe awatara = wynik `GET /api/advocate/answers/{uid}/speech`
(+ czasy słów) dla odpowiedzi, którą odbiorca może czytać; po cofnięciu grantu awatar milknie (404) jak TTS.
S17 etykiety i cytaty widoczne podczas mówienia (Chromium). S18 `prefers-reduced-motion` i opt-out wyłączają animację.
S19 telefon: 30 kl./s na urządzeniu klasy średniej przy `stylized_3d` (pomiar, nie obietnica). S20 polski lip-sync:
ocena osoby polskojęzycznej na 20 zdaniach testowych (synteza, dane syntetyczne), wynik zapisany jako pomiar.
S21 (tylko D) budżet: start instancji odmówiony, gdy `max_minutes_per_day` lub budżet przekroczony.

## 7. Pakiety

| ID | Zakres | Odbiór |
|---|---|---|
| P13a | `indicator` (B): amplituda z `<audio>` przez Web Audio, `avatar.*` w rejestrze ustawień | S16–S18 w Chromium |
| P13b | `stylized_3d` z amplitudą: three.js + własny GLB z kształtami ust, ładowany z `static/` | S16–S19 |
| P13c | czasy słów: TTS z SSML `<mark>` (v1beta1), trasa zwraca `timepoints` obok MP3 | test kontraktu na atrapie + 1 wywołanie syntetyczne |
| P13d | polskie reguły grafem→wizem, moduł lip-sync | S20 |
| P13e | (opcjonalny, decyzja właściciela) Unreal/MetaHuman + Pixel Streaming na żądanie | S21 + osobny projekt bezpieczeństwa strumienia |

## 8. Pytania tak/nie do właściciela

1. Czy awatar ma być **stylizowany** (rekomendacja), czy **fotorealistyczny** (Unreal/MetaHuman, droższy, wyższe ryzyko)?
2. Czy dla wariantu Unreal zgadzasz się na **budżet ponad 1 €/dzień** w dni jego użycia (rząd kilku USD/h GPU)?

## 9. Luki i niepewności (jawnie)

- Ceny GPU pochodzą z jednej promocyjnej strony dostawcy; brak oficjalnej ceny Pixel Streaming.
- Warunki licencji MetaHuman/UE podsumowane z [metahuman.com](https://www.metahuman.com/en-US/license); pełną EULA czytać przed wdrożeniem.
- Obsługa języków w TalkingHead podana według opisu repozytorium i jego kopii; nie sprawdzałem kodu ani licencji.
- Brak wizemów w Google TTS stwierdzony na podstawie dokumentacji `v1beta1`; nowsze wersje API mogą to zmienić.
