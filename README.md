# EMERLOG Urlopy / Leave Manager

Nowoczesna aplikacja webowa do obsługi urlopów i nieobecności w polskiej firmie logistycznej EMERLOG.

## Aktualny MVP

- logowanie użytkowników,
- role: pracownik, menedżer, kadry, admin,
- lewy panel boczny z menu zależnym od roli,
- dashboard z kafelkami,
- składanie wniosków urlopowych,
- automatyczne liczenie dni roboczych,
- pomijanie sobót, niedziel i polskich świąt,
- typy nieobecności: urlop, L4, bezpłatny, zdalna, delegacja itd.,
- zastępstwo opcjonalne,
- notatka o załączniku jako placeholder etapu 2,
- lista wniosków z filtrami,
- akceptacja, odrzucenie, anulowanie i cofnięcie do poprawy,
- limity urlopowe: limit roczny, zaległe, wykorzystane, oczekujące, dostępne,
- prosty kalendarz miesięczny,
- baza pracowników,
- raport miesięczny,
- eksport CSV,
- historia działań,
- lokalna baza SQLite `database.db`.

## Logo EMERLOG

Aplikacja szuka logo tutaj:

```text
static/emerlog-logo.png
```

Jeżeli pliku nie ma, pokazuje tekstowy napis EMERLOG. Logo nie jest generowane ani modyfikowane.

## Uruchomienie lokalne na Windows

```powershell
cd C:\projekty\Urlopy
git pull
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Jeżeli uruchamiasz pierwszy raz:

```powershell
cd C:\projekty
git clone https://github.com/pabloosss/Urlopy.git
cd Urlopy
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
| jan | jan123 | pracownik |
| anna | anna123 | menedżer |
| pawel | pawel123 | menedżer |
| ewa | ewa123 | admin/kadry |
| kadry | kadry123 | kadry |
| admin | admin123 | admin |

## Dane przykładowe

Działy:

- Spedycja,
- Księgowość,
- Kadry,
- IT,
- Zarząd.

Pracownicy testowi:

- Jan Kowalski — pracownik,
- Anna Nowak — menedżer,
- Paweł Pisarczyk — menedżer,
- Ewa Dusińska — kadry/admin.

## Ważne

Plik `database.db` nie jest wrzucany na GitHub, bo zawiera dane użytkowników i wnioski.

Jeżeli po dużej zmianie schematu coś się wywali na testach, można usunąć lokalny plik:

```powershell
Remove-Item database.db
python app.py
```

Aplikacja utworzy bazę od nowa z danymi testowymi.

## Etap 2

- pełne załączniki plikowe,
- powiadomienia e-mail,
- eksport XLSX,
- dokładniejsza historia zmian,
- edycja pracowników,
- edycja typów nieobecności z panelu,
- integracja Outlook/Teams.
