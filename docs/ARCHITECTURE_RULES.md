# Reguły architektury AGEDS

AGEDS rozwijamy jako audytowalny warsztat materiałów i wyników przetworzeń.
Wspólny mechanizm ma obsługiwać różne dane bez zamazania ich znaczenia. Oryginały,
ich pozyskania, obserwacje parsera, transkrypcje, cytaty i interpretacje mają
osobne kontrakty. Rozwój uwzględnia ekosystem, ale nie zakłada wspólnej bazy.

## Autorytet reguł

| Źródło | Lokator i zakres |
|---|---|
| U1 — bieżące zlecenie | Użytkownik, 2026-09-30: autonomiczne przejęcie rozwoju zgodnie z jego filozofią, w kontekście ekosystemu; rutynowe decyzje oddane agentom. |
| U2 — zarządzanie | Użytkownik, 2026-09-30: osobny wątek zarządzania i pytanie o komunikację przez historię. Wybrany protokół repo jest odpowiedzią projektową, nie cytatem wcześniejszej decyzji. |
| H1 — koncepcja AGEDS | Eksport OpenAI, rozmowa `6a55d929-c238-83ed-af2f-a6d7ca877fb8`, wiadomość użytkownika `bbb21693-07bf-4305-8371-52fe0a3877e1`, 2026-07-14: wklejona rekonstrukcja specyfikacji i jej rozszerzenia. Nie jest oryginalną rozmową kwietniową. |
| H2 — rozumowanie | Eksport OpenAI, rozmowa `6a55fb9f-72b0-83eb-bff0-81346b338487`, wiadomości użytkownika `bbb21af3-405d-4da5-b76c-1b5350e6356d` i `bbb21e5c-06fa-48c0-94e5-282cb48011f2`, 2026-07-14: wewnętrzne rozumowanie i znajdywanie; argumentacja przez przechodzenie po drzewie. |
| H3 — źródła | Odszukana historia z 2026-09-30, ujęta w początkowym raporcie przejęcia: samodzielny skan, także CSV/XLS/XLSX; zachowanie oryginałów i ich błędów; nazwy, numery i czasy są przesłankami, nie gwarantowanym stanem faktycznym. Nie odczytano pełnego surowego eksportu tej bieżącej rozmowy. |
| E1 — ekosystem | `ECOSYSTEM.md` na zdalnym commicie `99389ba2ba1a5873b4183b592c62f7aa666276c0`: koncepcyjne kierunki, dwukierunkowa współpraca i zachowanie odrębności. |
| P1 — metoda projektowania | Zastosowana umiejętność użytkownika `derive-design-rules`: wspólne struktury przetwarzania, jawne straty/dodatki, zachowanie opcjonalności i dyscyplina epistemiczna. Techniki operacjonalizacji są rekomendacjami; worked examples są ilustracjami, nie specyfikacją AGEDS. |
| C1 — ograniczenia | Bieżąca sesja: koordynator + sześć aktywnych agentów. Jest to ograniczenie wykonania, nie stała cecha architektury AGEDS ani sprawdzony globalny limit wszystkich wątków. |

`MUST` oznacza wymaganie źródłowe lub jawnie uzasadnioną konieczność kontraktu.
`SHOULD` oznacza rekomendację, `MAY` opcję. Nie przepisujemy sugestii asystenta
na historyczne wymaganie użytkownika. Reguły techniczne poniżej są wnioskami
projektowymi tam, gdzie źródło nie ustaliło szczegółów implementacji.

## Reguły i sprawdzenie

| ID / zakres | Reguła i status | Uzasadnienie / weryfikacja | Wyjątek lub ponowna decyzja |
|---|---|---|---|
| A01 / źródła | MUST zachować oryginały i konflikty; wymaganie H3. | Skan bez zapisów do źródła; checksum przed/po, kolizje nazw, błędny numer, sprzeczne daty. | Kopia do store lub przesłanie do workera to osobna operacja pozyskania, nie skan. |
| A02 / pochodzenie | MUST oddzielać zawartość od obserwacji jej pozyskania; konieczność wyprowadzona z H1/H3. | Ten sam hash w dwóch źródłach zachowuje oba pochodzenia; ponowny import nie mnoży zdarzeń. | Nie utożsamiać hash z autorstwem/prawdą ani scan observation z acquisition observation. |
| A03 / transformacje | SHOULD opisywać stratę, dodanie i odwracalność; preferencja U1/P1, operacjonalizacja projektowa. | Porównać XML→event i WAV→transcript oraz graniczny skan bez ingest. | Nowy format musi zadeklarować własną semantykę i ograniczenia. |
| A04 / wyniki modeli | MUST przypinać run i wersję wyniku; H1 oraz konieczność audytu. | Dwie próby mają odrębne runy; brak metadanych jest `unknown`; confidence języka nie opisuje treści. | Istniejących wyników bez runu nie uzupełniać fikcyjną historią. |
| A05 / wykonanie | MUST fenceować publikację tokenem ważnej lease; konieczność przy wielu workerach. | Równoczesny claim, odzyskanie po awarii, spóźniony worker i rollback publikacji. | SQLite/UTC jest odwracalnym wyborem tego przyrostu; nie gwarantuje odporności na dowolny skok zegara. |
| A06 / cytaty | MUST wskazywać konkretny transcript ID i zakres; konieczność rozdzielenia wersji. | Zmiana transkrypcji nie przepina wcześniejszego cytatu; zakres i quote sprawdzane wobec wybranej wersji. | Obecny segment nie stanowi jeszcze wyboru dokładnych słów ani oceny prawdziwości wypowiedzi. |
| A07 / pakiety | MUST jawnie opisywać zawartość i wyłączenia; konieczność audytu. | Odbiór pakietu sprawdza wersję schematu i referencje. | Obecny eksport metadanych nie zawiera gwarancji replay, podpisu ani dokładnego restore. |
| A08 / abstrakcje | SHOULD współdzielić mechanizm tylko przy zgodnej semantyce; U1/E1/P1. | Dwa konkretne przypadki i granica; zachowane jednostki, payload i pochodzenie. | Wspólna nazwa/graf/JSON nie dowodzi zgodnego znaczenia. |
| A09 / integracje | MAY dodać adapter partnera po małym teście korzyści; E1 to brainstorm. | Sprawdzić dwukierunkowy przepływ i zakres dostępu bez zmiany oryginałów. | Integracja nie narzuca jednej bazy, ontologii ani jednego interfejsu. |
| A10 / decyzje | SHOULD stosować odwracalny default i trigger zmiany; operacjonalizacja U1/P1. | Default, alternatywa i warunek zmiany zapisane przy zadaniu. | Podjąć decyzję od razu, jeśli odroczenie blokuje test albo kosztuje więcej. |
| A11 / twierdzenia | MUST oddzielać obserwację, interpretację, hipotezę i wybór wykonania; H1/E1. | Konflikt pozostaje widoczny; zgodność modeli nie jest niezależnością źródeł. | Nie narzucać współdzielonej liczbowej pewności bez jej semantyki i walidacji. |
| A12 / LLM | MAY wdrażać kolejne profile standardu jako odrębne zdolności; H1/H2. | Konkretne prompt/przypadek/sprawdzenie; wynik modelu przypięty do dowodów i parametrów. | Pięć warstw nie jest wdrożone przez sam ingest, ASR, cytat lub eksport. |

## Dwa przypadki i granica wspólnego mechanizmu

**SMS z XML:** parser odczytuje pola i surowe atrybuty, tworzy zdarzenie oraz
zachowuje lokator. Ustalenie osoby lub czasu wymaga odrębnego uzasadnienia.
Idempotencja zdarzeń nie pozwala usunąć nowej obserwacji pozyskania pliku.

**Nagranie WAV:** zachowane bajty są wejściem runu ASR. Tekst i segmenty to wynik
modelu, a cytat wybiera konkretną wersję. Nazwa nagrania i numer w nazwie mogą
pozostawać w konflikcie z billingiem. Nie poprawiamy oryginalnej nazwy.

W obu przypadkach przydatne są identyfikacja zawartości, lokatory, runy, wersje
i audyt. Zdarzenie komunikacyjne i segment wypowiedzi mają różne payloady.
**Przypadek graniczny:** skaner CSV/XLS/XLSX może odczytać komórkę i zaproponować
korelację, nie pozyskując kopii pliku do evidence store. Rekord skanera nie
zastępuje `source_observations` ani nie tworzy zweryfikowanej tożsamości.

## Kontrakty istotnych transformacji

| Wejście → wynik | Zachowane | Strata | Dodanie | Odwracalność / pochodzenie |
|---|---|---|---|---|
| XML/WAV → zachowana kopia | Bajty, checksum; osobno locatory i metadane pozyskania. | Nie każde znaczenie filesystem metadata jest przenośne; historyczny czas pozyskania może być nieznany. | Hash i obserwacja pozyskania. | Bajty dokładne po sprawdzeniu hash; pełna rekonstrukcja oryginalnego środowiska nie jest gwarantowana. |
| XML SMS → event | Surowe pola i referencja do materiału. | Projekcja nie odtwarza dokładnej serializacji XML. | Parsowana data/kierunek/tekst; błędy i założenia normalizacji jawne. | Event nie zastępuje XML; oryginał umożliwia ponowny parser. |
| WAV → transcript | Referencja i hash audio; tekst, zakresy segmentów/słów dostępne z adaptera. | Barwa, intonacja, dźwięki pozamowne; omyłki ASR i pominięcia. | Rozpoznane słowa, język i granice czasowe są wynikiem modelu. | Z tekstu nie odtwarza się audio; ponowna inferencja nie jest dokładnym replay. |
| CSV/XLS/XLSX → manifest skanu | Lokatory wierszy/komórek, odczytane wartości, zakres skanu i konflikty. | Formaty/wizualny układ/nieodczytane pola lub obszary; pokrycie ograniczone limitami. | Interpretacja nagłówka, numeru, czasu i korelacji jako hipotezy. | Manifest nie odbudowuje arkusza ani nie wykonuje formuł; raw plik pozostaje źródłem. |
| Transcript → cytat segmentowy | Transcript ID, zakres, tekst cytatu i jego hash. | Cytat nie zawiera całego kontekstu. | Selekcja fragmentu przez użytkownika/operację. | Wersja źródłowa pozwala sprawdzić cytat; nie regeneruje oryginalnego audio. |
| Stan → pakiet metadanych | Zadeklarowane rekordy i ich referencje. | Oryginalne media i środowisko wykonania nie muszą być w pakiecie. | Wersja schematu i opis zakresu. | Dokładny restore/replay/podpis są odrębnymi przyszłymi kontraktami. |

Kompozycja transformacji przenosi ich lokatory i ograniczenia. Kolejna operacja
nie odzyskuje informacji odrzuconej wcześniej. Nie mnożymy wskaźników confidence
bez modelu ich znaczenia i zależności.

## Ekosystem: mapowanie koncepcyjne

| Partner | Potencjalna zdolność / granica |
|---|---|
| ChatADHD | Rozmowa jako interfejs do materiału, adnotacji i zadań; dedykowany warsztat nadal użyteczny. |
| iOmatrix | Wprowadzanie i zaznaczanie; dwukierunkowy kontekst w wybranym zakresie, nie automatyczne współdzielenie całej pamięci. |
| Loom / LEM | Relacje, perspektywy i praktyki epistemiczne; własne osie i grafy zachowują znaczenie. |
| WatchDog | Metody pochodzenia, odtwarzania analiz i porównywania twierdzeń; różne kryteria dowodowe domen. |
| Archiwa / PixelSpace | Odkrywanie i reprezentowanie materiału; rekonstrukcja sceny pozostaje interpretacją. |

Te adaptery nie są wdrożone w tym przyroście. Dostęp i uprawnienia są częścią
kontraktu partnera, nie skutkiem samej obecności w ekosystemie.

## Opcjonalność i następne sprawdzalne kroki

| Decyzja | Default / zachowana alternatywa | Warunek ponownej decyzji |
|---|---|---|
| Store i kolejka | SQLite + lokalne zachowane kopie; możliwa inna baza i object store. | Mierzona potrzeba współbieżności, zdalnego dostępu albo retencji wykracza poza ten kontrakt. |
| ASR | Serwerowy faster-whisper za adapterem; native/on-device lub inny provider pozostają opcją. | Realny benchmark jakości, czasu, kosztu i dostępu do danych uzasadnia zmianę. |
| Android | Seed chooser i przekazanie wybranych nagrań do klienta; samodzielny skan SAF bez seed to następny zakres. | Build aplikacji i test wskazanego katalogu na urządzeniu potwierdzają cały przepływ. |
| Cytaty | Wersja + segment; później dokładne słowa i selektory. | Word timestamps oraz walidacja granic i zgodności tekstu. |
| Pakiet | Metadane bez obietnicy odtworzenia. | Dokładny eksport/reimport zachowuje ID, relacje, wersje i błędy; media i podpis mają jawny zakres. |
| LLM / pięć warstw | Zachowane wymagania H1 i rozumowanie H2; nieaktywne rozszerzenia. | Konkretny scenariusz i test metadanych, klasyfikacji, Live Explainer, custody oraz kontroli rozbieżności. |

Najbliższy odbiór obejmuje serwerowy skan, idempotentne importy, lease, wersje
wyników i przypięcie cytatu. Native SAF, test telefonu,
dokładne słowa oraz restore metadanych są oddzielnymi następnymi zadaniami.
Syntetyczny adapter ASR nie potwierdza jakości realnego rozpoznawania mowy.

## Stan implementacji po nocnym przyroście 2026-10-01

Historyczne defaulty powyżej opisują decyzję fundamentu. Bieżące rozszerzenia:

- Android: samodzielny SAF CSV/TSV/XLSX/WAV + inventory bez seed; dawny katalog
  pozostaje opcjonalny. XLS i część kodowań jawnie unsupported. Źródła read-only.
- Cytat: segment lub ciąg słów z konkretnej wersji. Dokładny tekst/hashes/indices
  nie oznaczają zweryfikowanej akustycznej precyzji ASR. Przy błędnych słowach
  zachowujemy raw i możliwy poprawny wybór segmentowy.
- Metadane: inertne archiwum SQLite z dokładnym kanonicznym eksportem zwrotnym;
  brak live restore, mediów, podpisu oraz wznowienia zadań. Format archiwum
  ma odrębne tabele, aby historyczny running job nie stał się wykonywalny.
- UI: tekst źródła jest danymi również w wyszukiwaniu i cytatach. HTML ze
  źródła nie jest zaufanym markupem interfejsu.

Weryfikacja i konkretne granice: `docs/NIGHT_QA.md`,
`docs/ANDROID_NIGHT_BUILD_RECEIPT.json`, `docs/METADATA_ARCHIVE.md`.
Te rozszerzenia nie deklarują działających adapterów innych projektów.
