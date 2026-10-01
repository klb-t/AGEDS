# Chromium — odbiór fali 4

Rzeczywisty headless Chromium, syntetyczne WAV i SQLite; 17 przypadków.
Receipt: `WAVE4_BROWSER_RECEIPT.json`, ze sprawdzonymi hashami wejść.

Zachowano 13 wcześniejszych przypadków: dokładny cytat starej wersji, pobranie
i niezależne sprawdzenie pakietu, spóźnione odpowiedzi, literalny HTML źródła
oraz faktyczne odtwarzanie HTMLAudioElement z zatrzymaniem zakresu.

Nowe przypadki: dokładne bajty HTTP Range/HEAD/If-Range; odmowa identyfikatora
artefaktu lub wersji DOM większego niż Number.MAX_SAFE_INTEGER przed wysłaniem
zapytania; odmowa niebezpiecznego albo niezgodnego ID w odpowiedzi transkryptu.
Brak zaokrąglania ID do innego materiału. To ograniczenie widoku przeglądarki;
backend i inertny pakiet nadal zachowują pełne identyfikatory SQLite.

Testy nie potwierdzają odsłuchu człowieka, alignmentu ASR, telefonu ani wdrożenia
adaptera innego projektu. Wcześniejsze receipty pozostały bez zmian.
