# Pakiet dla adwokata — krótka instrukcja dla właściciela

Stan: 2026-10-26, gałąź `claude/rzecznik-design`. Polecenia wykonujesz na VM wewnątrz kontenera:
`sudo docker compose -p ageds -f docker-compose.yml -f docker-compose.vm.yml exec api python -m app.cli …`
(poniżej skrót `ageds …`). Każde polecenie wypisuje JSON; tokeny widać tylko raz.

## Raz, na początek

```bash
ageds authz-owner-init --name "Twoje imię"     # zapisz token właściciela
ageds authz-mode tokens                         # od teraz bez tokenu nikt nic nie widzi
```

Bez `authz-mode tokens` każdy, kto dotrze do adresu, działa jako Ty.

## Udostępnienie (jedno polecenie)

```bash
ageds authz-invite --name "Mecenas …" --case-id 3 --case-id 5 \
    --base-url https://<twoja-domena> --expires-days 60 [--read-bytes]
```

- Tworzy konto adwokata, pakiet z tymi sprawami w trybie **snapshot** (zamrożone na teraz: później dodany materiał
  nie trafia do niego sam) i prawo do czytania tekstu oraz rozmowy z rzecznikiem. `--read-bytes` pozwala też odsłuchać
  nagrania.
- Wypisuje `invitation_link`. **Link jest hasłem**: wyślij go tylko adwokatowi. Adwokat go otwiera, loguje się bez
  wklejania i od razu widzi listę udostępnionych spraw.

## Zanim wyślesz: sprawdź, co zobaczy

```bash
ageds authz-preview --principal-uid <principal_uid z poprzedniego wyniku>
```

Pokazuje same nazwy spraw i materiałów oraz liczby wersji tekstu, bez treści.

## Później

| Chcę… | Polecenie |
|---|---|
| dodać nowy materiał do pakietu | `ageds authz-package-snapshot --package-uid <P>` (poszerza: zapisywane w audycie) |
| zobaczyć, co czeka poza snapshotem | `ageds authz-package-growth --package-uid <P>` |
| zabrać dostęp natychmiast | `ageds authz-revoke --grant-uid <G>` (pobrane kopie nie znikają) |
| unieważnić link | `ageds authz-token-revoke --method-uid <część tokenu przed kropką>` |
| włączyć odpowiedzi modelu | `ageds advocate-provider vertex` + `ageds advocate-consent --scope installation --basis "R100 …"` |

## Czego adwokat nie zobaczy

Twoich notatek (adnotacji), audytu, innych spraw, przypisań z innych spraw, Twoich rozmów z rzecznikiem
ani tego, czy materiał występuje gdzie indziej. Rozmów adwokata nie widzisz (ustawienie
`conversation.owner_visibility`, preset `none`; adwokat widzi to ustawienie na ekranie).

## Ograniczenia, które warto mu powiedzieć

Transkrypcje są rozpoznaniem mowy i mogą mieć błędy; etykiety („wynika z cytowanych fragmentów”, „wniosek”) opisują
rodzaj twierdzenia, nie pewność; wyszukiwanie dopasowuje słowa, nie znaczenie.
