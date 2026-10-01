# Fala 13 — niezależny odbiór kontraktu odpowiedzi natywnej

Zakres N84 jest ograniczony do kontraktu `SelectedCitation.requireMatchingCreated`.
Nie zmienia kodu produkcyjnego, modeli HTTP ani schematu serwera. Nowy zestaw
`CitationResponseAdversarialTest` sprawdza odpowiedź serwera wobec projekcji
zamrożonej przed żądaniem zapisu.

## Sprawdzane własności

- identyczny tekst i identyczny zakres po zaokrągleniu nie wystarczają do
  identyfikacji wystąpienia: inny indeks segmentu i inna referencja słowa są
  odrzucane;
- dodatni identyfikator cytatu, artifact ID, transcript ID, dokładny tekst oraz
  oba końce zakresu należą do kontraktu odpowiedzi;
- selektor ma dokładny zestaw pól i kanoniczne metadane; brak, nadmiar lub
  zmiana wartości są odrzucane;
- kolejność kluczy obiektu JSON jest nieistotna, lecz kolejność tablic indeksów
  i referencji pozostaje istotna;
- równoważne zapisy liczb, m.in. `0`, `0.0` i wykładnik, są zgodne semantycznie;
  napisy, wartości logiczne, `null` i różne duże liczby nie są koercjonowane;
- listy przekazane przez wywołującego i listy zwrócone w kolejnych obiektach
  żądania nie mogą zmienić zamrożonego wyboru;
- projekcja sekund na milisekundy używa zaokrąglenia do najbliższej wartości z
  remisem do parzystej dla granic 0.5 ms;
- poprawne odpowiedzi segmentowe i słowne przechodzą, w tym przy innej
  kolejności kluczy i równoważnym zapisie liczb.

## Granice dowodu

To są syntetyczne testy kontraktu rdzenia. Nie dowodzą wykonania żądania HTTP,
poprawności backendu, odtwarzania audio, alignmentu ASR ani działania na
telefonie. `quote_sha256` pozostaje wartością zwróconą przez serwer; ten matcher
nie rekonstruuje ani nie uwierzytelnia skrótu. Wynik wykonania całego zestawu JVM
zapisuje koordynator po zintegrowanym buildzie; sam przegląd źródła nie jest
zaliczeniem testów.
