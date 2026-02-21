# Dalsjöfors Hyrservice – Boknings- och betalningssystem

## 1. Systemöversikt
Dalsjöfors Hyrservice använder en webbapplikation för bokning av släp och betalning via Swish.

- Webbapplikation för bokning av släp
- Betalning via Swish
- Statushantering (PENDING -> PAID -> CONFIRMED)
- Automatisk kvittohantering via webhook
- Fel-/skaderapportering

Systemet hanterar endast boknings- och statusdata. Ingen kortdata eller annan känslig betaldata lagras i systemet.

## 2. Teknisk arkitektur
### Backend
- Python
- HTTP server
- SQLite databas
- Hostas på Render

### Frontend
- Statisk HTML/CSS/JS

### Integrationer
- Swish API (Commerce)
- Webhook via Google Apps Script

Flöde:

Kund -> Skapar bokning (PENDING)
-> Swish payment request
-> Swish callback/statuskontroll
-> Databas uppdateras (PAID)
-> Kvitto skickas

## 3. Swish Commerce-flöde
1. Bokning skapas med status PENDING.
2. Backend skapar payment request mot Swish API.
3. Swish returnerar payment reference.
4. Systemet väntar på callback eller gör statuspolling.
5. Vid PAID:
   - Bokning markeras CONFIRMED
   - Kvitto skickas

Förtydligande:

- Ingen betalningsinformation lagras.
- Endast Swish-referenser och status sparas.
- Systemet är förberett för mTLS (certifikatbaserad kommunikation).

Notering om intern statusmodell: implementationen använder `PENDING_PAYMENT` som intern bokningsstatus före bekräftelse, vilket motsvarar PENDING i flödesbeskrivningen ovan.

## 4. Säkerhet
- Alla secrets lagras som miljövariabler.
- `WEBHOOK_SECRET` används för webhook-validering i integrationsflödet.
- Admin-endpoints kräver Bearer-token.
- Ingen hemlig information finns i repo.
- Filuppladdning valideras (typ + storlek).
- Rate limiting på rapportfunktion.

Systemet är designat enligt principen: "Minsta möjliga lagring av kunddata".

## 5. mTLS-förberedelse
Systemet är arkitektoniskt förberett för Swish mTLS-certifikat. Certifikat och privata nycklar hanteras via säker miljökonfiguration i produktion. Ingen certifikatdata finns i repository.

## 6. Dataskydd
- Lagrar endast namn, telefon och e-post vid behov.
- Ingen kortdata hanteras.
- Bilder i felrapport skickas endast via webhook och lagras inte permanent i databasen.
- Ingen tredjepartsdelning av data.

## 7. Miljövariabler
| Variabel | Beskrivning |
| --- | --- |
| `SWISH_API_URL` | Bas-URL till Swish Commerce API (i nuvarande implementation används `SWISH_COMMERCE_BASE_URL`). |
| `SWISH_CERT_PATH` | Sökväg till klientcertifikat för mTLS (i nuvarande implementation `SWISH_COMMERCE_CERT_PATH`). |
| `SWISH_KEY_PATH` | Sökväg till privat nyckel för mTLS (i nuvarande implementation `SWISH_COMMERCE_KEY_PATH`). |
| `SWISH_CA_PATH` | Sökväg till CA-certifikat för verifiering av Swish endpoint i produktionsmiljö. |
| `WEBHOOK_SECRET` | Delad hemlighet för webhook-validering (i nuvarande implementation `NOTIFY_WEBHOOK_SECRET`). |
| `REPORT_WEBHOOK_URL` | Endpoint för fel-/skaderapporter och relaterade notifieringar. |
| `ADMIN_TOKEN` | Bearer-token som skyddar admin- och dev-endpoints. |
| `DATABASE_PATH` | Sökväg till SQLite-databas (standard är lokal `database.db`). |

## 8. Drift
- Python 3.13
- Render-hosting
- SQLite
- HTTPS via hostingplattform

## 9. Status
Systemet är produktionsklart för Swish Commerce-integration. Mock-flöde används i testmiljö. Produktion sker via mTLS när bankavtal är aktiverat.
