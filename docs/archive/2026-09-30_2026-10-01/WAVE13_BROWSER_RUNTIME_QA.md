# N83 — odbiór odpowiedzi zapisu cytatu w kliencie web

2026-10-01. Wykonano **6 testów produkcyjnych handlerów w Node**,
z minimalnym syntetycznym DOM i kontrolowanym `fetch`. Nie wykonano nowego
Chromium, HTTP ani rzeczywistego HTMLAudioElement w tej fali.

## Wykonany test

```sh
node --test server/tests/js/citation-save-handler.test.mjs
```

Node v24.19.0: 6/6 passed, 0 skipped. Moduł `citations.mjs` jest importowany
bez zmian i rejestruje prawdziwe handlery zdarzeń. Test nie odtwarza logiki
walidatora. Podstawione są jedynie interfejsy DOM, audio i transportu.

| Przypadek | Sprawdzony wynik |
|---|---|
| Inne wystąpienie słowa; ten sam tekst, wersja i zakres 0–2 ms | Brak sukcesu, wpisu historii, przycisków odtwarzania i eksportu. |
| Inny indeks segmentu przy identycznym tekście i zakresie | Taka sama odmowa. |
| Zmieniony zakres przy zgodnym selektorze | Brak publikacji wpisu. |
| Poprawna odpowiedź po odmowie z ponownym użyciem tego samego ID | Wpis zostaje przyjęty: odmowa nie zatruła zbioru ID pagera. |
| Zmiana kontrolek i obiektu transkrypcji podczas oczekiwania | Odpowiedź jest porównana z wyborem sprzed żądania. |
| Poprawna / błędna odpowiedź po wyborze nowej wersji | Poprawna trafia do historii artefaktu przypięta do starej wersji; błędna nie trafia. Obie zachowują status i podgląd nowej wersji. |

Pozytywna odpowiedź zawiera granice `.0005`–`.0025` s i kanoniczne
`0`–`2` ms. Test potwierdza integrację tych wartości w zapisanym przycisku,
nie akustyczną dokładność tak krótkiego zakresu. Żaden zapis nie uruchamia
odtwarzania automatycznie.

Te same sześć testów wykonano niezależnie na produkcyjnym module sprzed
poprawki, odczytanym przez `git show HEAD:server/app/static/citations.mjs`
z lokalnego commitu `ab8913365fa97d9ad7590a76284bb23157c5896a` do osobnego
katalogu tymczasowego: **4 failed, 2 passed**. Stary handler potwierdzał
zapis błędnego wystąpienia i dodawał niezgodny zakres oraz spóźnioną błędną
odpowiedź do historii. Nie zmieniano plików roboczych na czas tej próby.
To wykazuje regresję klienta przy kontrolowanej błędnej odpowiedzi;
nie wykazuje generowania błędnego selektora przez backend.

## Przygotowany test rzeczywistej przeglądarki

`server/tests/browser/acceptance.cjs` rozszerzono o cztery przypadki:

1. Odrzucenie symulowanej odpowiedzi POST z innymi referencjami słów.
2. Odrzucenie analogicznej odpowiedzi z innym indeksem segmentu.
3. Poprawny rzeczywisty zapis po odmowie, wraz z zaokrągleniem połówki ms.
4. Odrzucenie spóźnionej błędnej odpowiedzi po zmianie wersji, bez zmian
   historii, statusu nowej wersji ani automatycznego odtwarzania.

Dotychczasowy test poprawnej opóźnionej odpowiedzi pozostaje w zestawie.
Osobny syntetyczny artefakt ma dwa wystąpienia ` echo` o takich samych
czasach oraz nowszą wersję. Fałszywe odpowiedzi POST są wstrzykiwane
wyłącznie przez Playwright, bez zapisu ich do bazy. Parser składni JS i
kompilacja składni Python fixture przeszły. **Te cztery przypadki Chromium
nie zostały wykonane w tej fali**.

## Zaobserwowana granica środowiska

Dostępny jest Playwright 1.62.1 w runtime, lecz `chromium.launch()` zakończył
się komunikatem o brakującym pliku:

```
/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell
```

Nie znaleziono executables Chrome/Chromium w `/opt`, `/usr` ani
`/root/.cache`. Nie pobierano przeglądarki. Bazowy Python i Python runtime
nie miały FastAPI, uvicorn, Jinja2 ani multipart; koordynator przygotował
osobno dostępne zależności backendu i sprawdził tworzenie syntetycznej bazy.
Tamten test fixture nie jest testem serwera HTTP ani przeglądarki.

Po udostępnieniu przeglądarki oraz zależności serwera uruchomić cały zestaw
z `AGEDS_BROWSER_BUDGET_FIXTURE=1`, właściwym `AGEDS_BROWSER_EXECUTABLE`
i **nowym** plikiem `AGEDS_BROWSER_RECEIPT`. Nie nadpisywać historycznych
receiptów Chromium z wcześniejszych fal i nie przypisywać im nowego kodu.
