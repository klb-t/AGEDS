# AGEDS — zasady pracy agentów

Najpierw przeczytaj `HANDOFF.md`, bieżący indeks wskazany w nim (obecnie
`coordination/night-20261001.json`), `coordination/state.json`, `coordination/README.md`,
`docs/ARCHITECTURE_RULES.md` i `ECOSYSTEM.md`. Jeśli `HANDOFF.md` nie jest jeszcze
dostępny, użyj pozostałych plików i jawnie odnotuj brak. Stan z historii czatu
sprawdzaj wobec aktualnego kodu i commitów.

## Autorytet i autonomia

- Bieżąca wypowiedź użytkownika ma pierwszeństwo w swoim zakresie. Odróżniaj
  wymaganie użytkownika, ograniczenie, brainstorm, wniosek i własną propozycję.
- Użytkownik powierzył rutynowe decyzje rozwojowe agentom. Implementuj, testuj
  i integruj odwracalne zmiany bez ponownego proszenia o zgodę.
- Eskaluj strategiczną zmianę celu lub zakresu wykraczającą poza autoryzację,
  nieautoryzowaną operację nieodwracalną, nowy istotny koszt albo zobowiązanie
  wobec zewnętrznego podmiotu. Strategia techniczna w powierzonym zakresie
  pozostaje autonomiczna; zastana autoryzacja nadal obowiązuje.
- Nie przypisuj użytkownikowi nowego wymagania tylko dlatego, że pasuje do
  ogólnej filozofii. Nie traktuj sugestii wcześniejszego asystenta jako decyzji.
- Nie obiecuj pracy po zakończeniu aktywnego wykonania ani nieweryfikowanej
  komunikacji z innym wątkiem.

## Współpraca

- W tej sesji limit to **koordynator + sześć aktywnych agentów**. Role zarządzania
  rotują w tych samych sześciu slotach; nie są dodatkowymi sześcioma procesami.
- Każde zadanie ma stabilny ID, właściciela, zakres plików, wynik, kryterium
  odbioru i lokator źródła/commitu. Nie edytuj plików innego właściciela bez
  uzgodnienia; potrzebny kontrakt przekaż właścicielowi.
- Integracja zmian wspólnych jest sekwencyjna. `coordination/state.json` aktualizuje
  koordynator po sprawdzeniu wyników; wynik lokalny nie oznacza wdrożenia.
- Osobny wątek zarządzania może korzystać z rekordów w repo i potwierdzonych
  commitów. Wyszukiwanie historii pomaga odzyskać kontekst; nie gwarantuje
  doręczenia, kolejności, aktualności ani wykonania zadania.
- Nie deklaruj innego wątku jako uruchomionego, dopóki nie ma takiej obserwacji.
  Odbiór zadania i wyniku wymaga jawnego potwierdzenia zgodnie z protokołem.

## Niezmienniki danych

- Skan źródeł jest tylko do odczytu. Nie naprawiaj nazw, numerów, dat ani treści
  oryginału. Zapis metadanych kieruj poza wskazany katalog źródłowy.
- `scanner.observations` opisują odczytane wartości i hipotezy korelacji.
  `source_observations` w SQLite opisują pozyskania zachowanych bajtów. Te rekordy
  mają różną semantykę; nie utożsamiaj ich na podstawie nazwy „obserwacja”.
- Hash identyfikuje zawartość i pozwala porównać integralność; sam nie dowodzi
  prawdziwości, autorstwa ani czasu powstania. Oddziel zawartość od pochodzeń.
- Surowy materiał, wynik parsera/ASR, adnotacja i interpretacja pozostają osobne.
  Konflikt zachowuj z lokatorami obu źródeł, bez automatycznego rozstrzygnięcia.
- Każda próba modelu ma osobny run i metadane; brak modelu/wersji/parametru
  oznacz jako `unknown`. Kolejny ukończony run daje nowy wynik.
- Wynik publikuje tylko właściciel ważnej lease, atomowo z zakończeniem zadania.
  `language_probability` nie jest pewnością poprawności transkrypcji.
- Cytat wskazuje konkretną wersję wyniku i zakres. Selektor słowny wymaga
  zapisanych znaczników ASR; żaden selektor nie dowodzi poprawności alignmentu,
  odsłuchu ani prawdy wypowiedzi.
- Pakiet metadanych nie jest kompletnym replay, podpisem ani bezpiecznym restore.
  Nie opisuj planowanej zdolności jako dostępnej.

## Architektura i odbiór

- Wyodrębniaj wspólne mechanizmy dopiero po porównaniu dwóch konkretnych
  przypadków i przypadku granicznego. Zachowuj typy, jednostki, czas i semantykę.
- Dla istotnych transformacji określ zachowane informacje, straty, dodane
  założenia, odwracalność i pochodzenie. Nie wymyślaj liczbowej pewności.
- Wybieraj mały, odwracalny domyślny wariant i konkretny warunek ponownej
  decyzji. Opcjonalność ma umożliwiać postęp.
- Mapa ekosystemu jest koncepcyjna. Nie narzuca wspólnej bazy, ontologii,
  technologii ani automatycznego dostępu do prywatnych danych.
- Testy używają trudnych danych syntetycznych. Nie używaj prywatnych materiałów
  ani modeli wymagających dużego pobrania bez odpowiedniego zakresu zadania.
- Uruchom testy adekwatne do zmiany i podaj ich faktyczny zakres. Build Android,
  adapter syntetyczny i realna inferencja ASR są odrębnymi dowodami.
- Przed migracją zatrzymaj stare procesy workerów i wykonaj sprawdzalny backup.
  Stary załadowany kod nie stosuje nowego fencing. Nie usuwaj starych wyników.
