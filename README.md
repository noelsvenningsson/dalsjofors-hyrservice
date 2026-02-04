# Dalsjöfors Hyrservice – Bokningssystem (Milestone C Preview)

Den här versionen bygger vidare på Milstolpe B och introducerar en
förhandsversion av Milstolpe C.  Förutom den flerstegsbaserade
bokningsguiden från Milstolpe B innehåller projektet nu:

* **Förbättrat användargränssnitt** – modernare design, större
  marginaler och tydligare knappar.  Företagsuppgifter och kontaktinfo
  visas i sidfoten på alla sidor.
* **Fix för visning av tillgänglighet** – Steg 1 visar inte längre
  “0/2 lediga” eller “Fullbokat” innan användaren valt både hyrestid
  och datum (och starttid för tvåtimmarsbokningar).  Räknarna
  uppdateras först när en slot är vald.
* **Betalningssidor** – när kunden bekräftar sin bokning skapas en
  reservation och användaren omdirigeras till en betalsida under
  `/pay?bookingId=<id>`.  Sidan visar en QR‑kod för Swish och
  information om belopp och meddelande.  Efter betalning kan kunden
  öppna `/confirm?bookingId=<id>` för en bekräftelsesida med kodlås 6392
  och kopierbar text.
* **Förberedelser för Swish Commerce API** – servern har nya
  API‑endpoints (`/api/payment`, `/api/swish/callback`) som genererar
  (simulerade) betalningsreferenser och tar emot callbacks från Swish.
  Bokningar får ett automatiskt utgångsdatum (10 minuter) och städas
  upp vid varje förfrågan.  Den riktiga API‑integrationen kräver cert
  och publik server och kommer i nästa iteration.

Koden är fortfarande helt i Python och använder endast
standardbiblioteket (ingen Flask/FastAPI), och SQLite lagrar all data
i `database.db`.  Filen `db.py` har utökats med kolumnerna
`swish_id` och `expires_at` samt funktioner för att förfalla och
hämtning av bokningar.

## Förutsättningar

* Python 3.11 eller senare.  Inga externa beroenden behövs – allt
  använder standardbiblioteket.

## Installation och start av servern

1. Klona eller packa upp katalogen `dalsjofors-hyrservice`.
2. Öppna en terminal i katalogen och initiera databasen (första gången):

   ```sh
   python3 -c "import db; db.init_db(); print('Databasen är initierad')"
   ```

   Detta skapar filen `database.db` om den saknas.  Du kan köra samma
   kommando flera gånger utan att data skrivs över.

3. Starta webbservern med ett enda kommando:

   ```sh
   python3 app.py
   ```

   Servern lyssnar på port 8000 (eller den port som sätts i
   miljövariabeln `PORT`). När servern körs ser du raden
   `Running Dalsjöfors Hyrservice on http://localhost:8000`.

4. Öppna din webbläsare och gå till `http://localhost:8000`.  Nu visas
   bokningsguiden.

När du har genomfört en bokning och klickat “Fortsätt till betalning”
kommer du att landa på `/pay?bookingId=<id>` där en QR‑kod för Swish
visas.  Betalningssidan och bekräftelsesidan är dynamiskt genererade
och finns inte som separata filer i `static/`.

Du kan testa live API:er separat med `curl` eller liknande.  Exempel:

```sh
# Beräkna pris för heldag på ett visst datum
curl "http://localhost:8000/api/price?rentalType=FULL_DAY&date=2026-02-04"

# Kontrollera tillgänglighet för gallersläp 2 timmar 4 februari 2026 kl 10:00
curl "http://localhost:8000/api/availability?trailerType=GALLER&rentalType=TWO_HOURS&date=2026-02-04&startTime=10:00"

# Skapa en boknings‑reservation (hold)
curl -X POST -H "Content-Type: application/json" \
  -d '{"trailerType":"GALLER","rentalType":"TWO_HOURS","date":"2026-02-04","startTime":"10:00"}' \
  http://localhost:8000/api/hold

# Generera en betalningsförfrågan (falsk Swish‑request)
curl "http://localhost:8000/api/payment?bookingId=<bokningsId>"
```

## Använda API:erna från kod

Förutom webbgränssnittet kan du använda funktionerna i `db.py` direkt
i egna skript.  Följande exempel demonstrerar hur man skapar en
bokning för en tvåtimmarsperiod:

```python
from datetime import datetime, timedelta
import db

# Initiera databasen om det inte redan gjorts
db.init_db()

# Bestäm start‑ och sluttid
start = datetime(2026, 2, 4, 10, 0)
end = start + timedelta(hours=2)

# Beräkna pris och kontrollera tillgänglighet
price = db.calculate_price(start, "TWO_HOURS")
if db.check_availability("GALLER", start, end):
    booking_id, actual_price = db.create_booking("GALLER", "TWO_HOURS", start, end, price)
    print(f"Bokning skapad med id {booking_id} och pris {actual_price} SEK")
else:
    print("Fullbokat för vald tid")
```

## Projektstruktur

```
dalsjofors-hyrservice/
├── app.py        – HTTP‑server som exponerar API:er och statiska filer
├── db.py         – Datamodell och affärslogik (init_db, calculate_price, check_availability, create_booking, m.m.)
│   │               – Har utökats med `swish_id`, `expires_at`, `expire_outdated_bookings`,
│   │                 `get_booking_by_id` och `set_swish_id` för betalningsflöde
├── index.html    – Start‑sida med bokningsguiden
├── static/
│   ├── app.css   – Grundläggande stilar för wizardsidorna
│   └── app.js    – JavaScript som styr flödet, hämtar pris och tillgänglighet
├── qrcodegen.py  – Extern modul (MIT‑licens) för att generera QR‑koder (används i senare milstolpar)
├── utils.py      – Hjälpfunktioner, bl.a. `to_svg_str` för att skapa SVG av en QR‑kod
├── README.md     – Denna fil
└── database.db   – SQLite‑databas (skapas automatiskt vid init)
```

## Checklista krav (Milstolpe B)

| Krav                                                                | Uppfyllelse |
|--------------------------------------------------------------------|------------|
| Wizard med fyra steg (släp, hyrestid, datum/tid, sammanfattning)     | ✅ `index.html` + `app.js` visar steg och navigering |
| Live pris: 2 h alltid 200 kr, heldag 250 / 300 kr beroende på dag   | ✅ API `/api/price` och frontenden uppdaterar pris beroende på datum |
| Live tillgänglighet: visar antal lediga av 2 och hindrar fullbokning | ✅ API `/api/availability` returnerar `remaining` och UI visar och spärrar `Nästa` |
| Ingen dubbelbokning / överlapp                                      | ✅ `check_availability` + räknande i `/api/availability` |
| Skapa boknings‑reservation (hold) via API                           | ✅ POST `/api/hold` skapar `PENDING_PAYMENT`‑bokning och returnerar id + pris |
| Mobilvänligt och tydligt gränssnitt                                 | ✅ Enkel responsiv design i CSS |
| Dev‑läge med ?dev=1 visar debug och knappar                         | ✅ Dev‑panel visar start/end, remaining och kan skapa testbokning |
| 1 kommando start (python3 app.py)                                   | ✅ Server startas med ett kommando |
| Data sparas i SQLite                                                | ✅ `database.db` kvarstår mellan sessioner |

## Kända begränsningar

* Betalningsflödet och Swish‑QR‑koder implementeras i Milstolpe C.
* Ingen administrationspanel ännu – kommer i Milstolpe D.
* Databasen kan inte rensas via UI än (dock finns dev‑knapp för att skapa
  testbokning). Detta kommer i kommande iteration.

## Nästa steg

Milstolpe B kommer att bygga ett flerstegsformulär för användarna där de
kan välja släp, hyrestid, datum/tid och få en sammanfattning med pris och
betalinformation i en enkel webbapplikation.