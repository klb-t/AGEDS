# Android — odbiór fali 3

Izolowany build 2026-09-30 23:46–23:48 UTC: 57 zadań Gradle wykonanych,
116 testów JVM (86 Android, 30 desktop), bez błędów i pominięć.
Sprawdzono 56 hashy wejściowych; źródła nie zmieniły się podczas builda.
Receipt: `ANDROID_WAVE3_BUILD_RECEIPT.json`.

Nowy zakres: wybór konkretnej wersji transkrypcji, dokładny cytat segmentowy
lub słowny, zapis cytatu i odtwarzanie jego zakresu. Testy obejmują spóźnione
odpowiedzi po zmianie serwera/materiału/wersji, cykl odtwarzacza, granice
czasu, ograniczenie projekcji do 10000 słów oraz limity selektorów.

APK debug: 14124053 bajty, SHA-256
`b6f085dc5579e96f1a44e18281379aba9649e081e6db0fd226f7de549fbce572`.
Weryfikacja podpisu apksigner zakończyła się kodem 0.

Nie wykonano testu telefonu, SAF runtime ani odsłuchu. MediaPlayer i Compose
zostały skompilowane; testy JVM używają kontrolowanych atrap audio/serwisu.
Przybliżone przewijanie nie dowodzi zgodności akustycznej znaczników ASR.
