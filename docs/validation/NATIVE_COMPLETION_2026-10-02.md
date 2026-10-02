# Native completion — 2026-10-02

**Pełny odbiór przypiętego builda przeszedł: 411 testów JVM, 0 błędów i 0
pominięć, debug APK oraz kompilacja aplikacji testowej SAF.** N80/N81/N84 są
wykonane. Odbiór telefonu i uruchomienie instrumentation pozostają otwarte.

| Zakres | Wynik |
|---|---|
| Core desktop JVM | 135 testów przeszło |
| Android JVM | 276 testów przeszło |
| Fala 13, zawarta w 411 | 29 testów dokładnego selektora i odpowiedzi zapisu przeszło |
| Prywatność presetów, zawarta w 411 | 3 testy konfiguracji seed przeszły |
| SAF instrumentation | 6 przygotowanych testów skompilowano; wykonanych runtime 0 |
| Telefon / picker / uprawnienia | Nie badano; brak urządzenia i emulatora |

## Odtworzenie i dowody

Z pełnym JDK, przypiętym Android SDK i dostępem do zależności:

```bash
scripts/build_verify.sh /tmp/ageds-native-result --include-instrumentation
```

Ustaw `JAVA_HOME`, `ANDROID_HOME` i opcjonalnie `GRADLE_USER_HOME`. Katalog
wynikowy musi być pusty. Helper kopiuje bieżące wejścia do osobnego snapshotu,
odrzuca błędy i pominięte testy, sprawdza podpis APK i zachowuje wyniki każdego
zestawu. `--include-instrumentation` kompiluje test APK; uruchomienie na
urządzeniu wymaga osobnego `scripts/verify_saf_provider.sh --run`.

Finalny build wykonał 87 zadań Gradle. Wszystkie 95 hashy wejść zgadzają się
z aktualnym repozytorium. Źródła były niezmienione w czasie finalnego odbioru.
Użyto Gradle 9.7.0, Kotlin 2.4.20, AGP 9.4.0, serialization 1.11.0 i JDK
21.0.12.1. `compileSdk = 37` pozostało bez zmian; oficjalny pakiet platformy
to `platforms;android-37.0`. Zachowano przypięte wersje projektu.

Debug APK: **14 173 205 bajtów**,
SHA-256 `ef53cb3cde477fdc43e1d990bef75b2d41c168e348b670dea1625b568303a83d`.
Test APK: **407 235 bajtów**,
SHA-256 `5a30a8be97feddba2a108992a1724429288b1f6982528f5ecdf66e85e311f373`.
Podpis obu plików zweryfikowano z kodem 0. Pliki APK nie są dodane do git.

- [Bieżące rozliczenie](NATIVE_COMPLETION_2026-10-02.json)
- [Surowy receipt finalnego builda i hashy źródeł](NATIVE_BUILD_2026-10-02.json)
- [Pierwsza nieudana próba](NATIVE_FIRST_ATTEMPT_2026-10-02.json)
- [Pomocniczy odbiór core JVM](NATIVE_CORE_FALLBACK_2026-10-02.json)

## Wykryty i naprawiony błąd testu

Pierwszy rzeczywisty odbiór core ujawnił wadę przygotowanego testu
`selectionAndRequestsKeepDefensiveCopiesOfCallerLists`. Jednoelementowa lista
z `toList()` jest na JVM niemodyfikowalna, ale jej rzutowanie na `MutableList`
może się powieść. `clear()` rzucało `UnsupportedOperationException`, zanim test
sprawdzał odseparowanie kopii. To samo niepowodzenie potwierdził pierwszy
przypięty build: 135 testów, 1 błąd.

Commit `19af439` zmienił dane testowe na dwa elementy. Test rzeczywiście
modyfikuje teraz zwrócone kopie indeksów i referencji słów, a następnie sprawdza
zamrożony wybór oraz dokładną odpowiedź. Nie zmieniano kodu produkcyjnego
selektora, aby obchodzić wadę testu. Finalny czysty snapshot przeszedł wszystkie
411 testów. Pierwsza próba zachowuje `source_unchanged = false`, ponieważ w tym
czasie wykonano zapowiedzianą poprawkę testu i przeniesiono niewpięty prototyp
C++ poza katalog produkcyjny; nie jest ona odbiorem końcowym.

Pomocniczy odbiór core użył Kotlin 2.4.0, rzeczywistego JUnit oraz JDK 21:
po poprawce przeszedł 135 testów. Jego zakres i wersję zapisano osobno.
Ostateczny wynik pochodzi z przypiętego Gradle i Kotlin 2.4.20.

## Granice

`adb devices` wykazało pustą listę. Brakuje emulatora, obrazów systemu i KVM.
Kompilacja sześciu testów SAF nie oznacza ich wykonania, a testy JVM nie
potwierdzają picker UI, cyklu uprawnień, transportu HTTP, jakości ASR,
alignmentu ani prawdziwości wypowiedzi. APK ma podpis debug; nie wykonano
publikacji w sklepie. Odbiory backendu, przeglądarki i realnego ASR mają
oddzielne dowody.
