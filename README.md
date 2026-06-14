# Urlopy Firmowe

Prosta aplikacja webowa do obsługi wniosków urlopowych w firmie.

## Aktualny zakres

- logowanie użytkowników,
- role: pracownik, kadry, admin,
- składanie wniosków urlopowych,
- automatyczne liczenie dni roboczych,
- pomijanie sobót, niedziel i polskich świąt,
- limity urlopowe: przysługuje / zaakceptowane / oczekujące / dostępne,
- blokada składania wniosku ponad dostępny limit urlopu,
- anulowanie oczekującego wniosku,
- lista własnych wniosków dla pracownika,
- lista wszystkich wniosków dla kadr/admina,
- akceptowanie i odrzucanie wniosków,
- odświeżony panel graficzny,
- lokalna baza SQLite `database.db`.

## Uruchomienie lokalne na Windows

```powershell
cd C:\projekty\Urlopy
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Jeżeli uruchamiasz pierwszy raz:

```powershell
cd C:\projekty\Urlopy
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Potem wejść w przeglądarce:

```text
http://127.0.0.1:5000
```

## Konta testowe

| Login | Hasło | Rola |
|---|---|---|
| admin | admin123 | admin |
| kadry | kadry123 | kadry |
| jan | jan123 | pracownik |

## Ważne

Plik `database.db` nie jest wrzucany na GitHub, bo zawiera dane użytkowników i wnioski.

Z puli urlopowej odejmują się obecnie tylko typy:

- Wypoczynkowy,
- Na żądanie.

Pozostałe typy, np. L4, Bezpłatny, Okolicznościowy, Odbiór godzin, nie zmniejszają limitu urlopu.

## Plan dalszych funkcji

- panel admina do dodawania pracowników,
- działy i menedżerowie,
- komentarz kadrowej przy odrzuceniu,
- historia zmian statusu,
- kalendarz nieobecności,
- eksport do Excela,
- powiadomienia e-mail.
