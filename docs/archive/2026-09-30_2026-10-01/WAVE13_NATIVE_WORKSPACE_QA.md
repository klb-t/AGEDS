# Wave 13 — niezależny odbiór zapisu cytatu w Androidzie

## Zakres

Odbiór dotyczy wyłącznie granicy pomiędzy zamrożonym wyborem
`SelectedCitation` a odpowiedzią `Citation` otrzymaną przez
`CitationWorkspace.save()`. Testy używają syntetycznego transkryptu z dwoma
identycznymi fragmentami o takim samym tekście i takim samym zakresie czasu.
W tym przypadku dopiero selektor (`indices`) rozróżnia wystąpienia.

Nie zmieniono kodu produkcyjnego. Nowe niezależne testy znajdują się w:

- `androidApp/src/test/java/dev/klbt/ageds/CitationWorkspaceResponseAdversarialTest.kt`

## Odtworzona luka bazowa

Inspekcja bazowego `CitationWorkspace.save()` wykazała, że przed zmianą
odpowiedź była porównywana z wyborem tylko po `artifactId`, `derivedTextId`,
`quoteText`, `startMs` i `endMs`. Nie sprawdzano dodatniego identyfikatora ani
kanonicznego selektora. Odpowiedź wskazująca `indices: [1]` mogła więc przejść
kontrolę wyboru `indices: [0]`, jeżeli oba wystąpienia miały ten sam tekst i
zakres czasu. Po akceptacji kod resetował stronę historii i publikował komunikat
o sukcesie.

To jest odtworzenie na poziomie ścieżki kodu oraz deterministycznego
kontrprzykładu testowego; w tym środowisku nie ma lokalnego kompilatora Kotlin,
Android SDK ani cache zależności, więc testów JVM tutaj nie uruchomiono.

## Przypadki odbioru

| Przypadek | Oczekiwany skutek |
|---|---|
| Ten sam tekst i czas, ale `indices: [1]` zamiast wybranego `[0]` | Odrzucenie przed resetem historii i komunikatem sukcesu; dotychczasowa strona pozostaje bez zmian |
| Odpowiedź zgodna z wyborem, ale `id = 0` | Odrzucenie; dotychczasowa strona pozostaje bez zmian |
| Dodatni ID i dokładny kanoniczny selektor `[0]` | Akceptacja, odświeżenie historii i komunikat z zapisanym ID |
| Poprawna, lecz spóźniona odpowiedź po zmianie wyboru na `[1]` | Brak zmiany historii, komunikatu i błędu; aktywny pozostaje nowszy wybór |

Pierwsze dwa testy sprawdzają również kolejność skutków ubocznych: licznik
odczytów historii nie może wzrosnąć po niezgodnej odpowiedzi. Odrzucenie nie
usuwa już widocznej strony cytatów.

## Granice dowodu

- Testy dotyczą lokalnej logiki JVM i sztucznej implementacji `CitationService`;
  nie są testem telefonu, transportu HTTP ani serwera.
- Poprawność selektora oznacza zgodność odpowiedzi z zamrożoną projekcją
  transkryptu. Nie dowodzi jakości ASR, alignmentu, odsłuchu ani prawdziwości
  wypowiedzi.
- Wykonanie testów musi zostać potwierdzone przez zintegrowany build w
  środowisku z przypiętym Android SDK i zależnościami. Samo dodanie źródła testu
  nie stanowi takiego potwierdzenia.
