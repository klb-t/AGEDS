# Koordynacja AGEDS

Repo przechowuje sprawdzalny stan pracy. `state.json` jest indeksem, nie jedynym
dowodem wykonania. Wyniki odsyłają do kodu, testów i commitów. Punkt wyjścia zdalnego
repo to `99389ba2ba1a5873b4183b592c62f7aa666276c0`; lokalny commit odtworzenia
nie zastępuje tej informacji.

## Osobny wątek zarządzania

Użytkownik wybrał oddzielny wątek zarządzania. Ten protokół umożliwia mu współpracę
z wątkiem implementacji przez **commity i stabilne ID**. Nie utworzono ani nie
zweryfikowano tu dodatkowego wątku. Każdy wątek musi mieć dostęp do odpowiedniej
rewizji repo; komunikacja wymaga wykonania odczytu przez odbiorcę.

Historia czatów służy odszukaniu wypowiedzi i decyzji. Wyszukiwanie może zwrócić
fragment, starszą wersję albo nie znaleźć nowego wpisu. Nie jest kolejką zadań,
potwierdzeniem doręczenia ani gwarancją kolejności. Brak odpowiedzi nie oznacza
zgody lub zakończenia pracy.

Limit znany dla bieżącej sesji: siedem procesów, czyli koordynator i sześć agentów.
Przydziały wykonawcze i zarządcze rotują w sześciu slotach. Nie zakładamy, że inny
wątek dodaje sloty lub omija limit; jego dostępny limit sprawdza tamten wątek.

## Protokół przekazania

1. **Odczyt:** koordynator sprawdza rewizję repo i `state.json`, identyfikuje źródła
   oraz status istniejącego zadania. Zachowuje `based_on_remote_commit`.
2. **Zlecenie:** zapisuje rekord `AGEDS-TASK-YYYYMMDD-NNN` z zakresem, właścicielem,
   zależnościami, kryterium odbioru i bazowym commitem. Zmiana priorytetu lub zakresu
   ma nowy rekord decyzji; nie usuwa poprzedniego uzasadnienia.
3. **Publikacja:** zlecenie staje się widoczne drugiemu wątkowi po udostępnieniu
   commitu w uzgodnionym repo/branchu. Lokalny plik lub commit nie wystarcza do
   deklaracji doręczenia. Zdalny push jest odrębną operacją, którą należy potwierdzić.
4. **Odbiór:** wykonawca potwierdza task ID, odczytany commit i własność plików.
   Bez potwierdzenia zadanie pozostaje oczekujące. Bezpośrednie wiadomości agentów
   w jednej sesji mogą przyspieszyć pracę, ale wynik utrwalamy w repo.
5. **Wykonanie:** `in_progress` → wynik `AGEDS-RESULT-YYYYMMDD-NNN`, zawierający
   task ID, commit/diff, faktyczne testy, ograniczenia i blokery. `done` oznacza
   gotowość do odbioru; `accepted` oznacza sprawdzony i zintegrowany wynik.
6. **Odbiór wyniku:** koordynator zapisuje potwierdzenie z task/result ID oraz
   rewizją. Aktualizuje indeks sekwencyjnie. Konflikty lub brak kontraktu dają
   `blocked`, z opisem warunku odblokowania.

Statusy: `proposed`, `ready`, `in_progress`, `blocked`, `done`, `accepted`.
Wersja schematu stanu: `1`. Ten przyrost zaczyna od rekordów w `state.json`;
większy backlog może użyć osobnych plików pod `coordination/tasks/`,
`coordination/results/` i `coordination/decisions/`, z zachowaniem tych samych ID.
Koordynator uzgadnia właściciela przed równoczesną edycją indeksu.

Minimalny rekord zadania:

```json
{
  "id": "AGEDS-TASK-20260930-008",
  "status": "proposed",
  "owner": "unassigned",
  "based_on_commit": "commit sprawdzony przy przydziale",
  "scope": ["jawnie uzgodnione pliki/moduły"],
  "acceptance": ["sprawdzalny warunek zachowania"],
  "depends_on": [],
  "authority": "explicit | constraint | inferred | proposed",
  "source_refs": [],
  "result_id": null,
  "received_at_commit": null
}
```

Role zarządzania: priorytety produktu, kontrakty architektury, przydział i
integracja, niezależny odbiór, pochodzenie wymagań oraz krytyka i eksperymenty.
Są odpowiedzialnościami rotującymi, a nie sześcioma dodatkowymi stałymi agentami.

## Decyzje i źródła

`explicit` to odnaleziona wypowiedź użytkownika w podanym zakresie; `constraint`
to zaobserwowane ograniczenie techniczne. `inferred` oznacza wyprowadzony wniosek,
a `proposed` wariant do oceny. Brainstorm i otwarta sprzeczność pozostają jawne.
Kod potwierdza zachowanie dopiero w zakresie odczytu/testu, nie przez sam opis.

Rutynowe wybory i odwracalne implementacje prowadzi koordynator autonomicznie.
Użytkownik dostaje konkretne wyniki, istotne niepewności i decyzje wymagające jego
udziału: strategiczne zmiany celu lub zakresu poza autoryzacją, nieautoryzowane
operacje nieodwracalne, nowy istotny koszt lub zewnętrzne zobowiązanie. Strategia
techniczna w powierzonym zakresie pozostaje autonomiczna. Nie tworzymy obowiązku
codziennych zatwierdzeń ani ponownej zgody na już autoryzowaną pracę.

Odczyt `state.json` lub pojedynczego commitu nie stanowi automatycznego uruchomienia
pracy. Dokument nie ustanawia schedulera ani obietnicy aktywności w tle.
