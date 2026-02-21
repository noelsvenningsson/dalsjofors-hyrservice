# Swish Commerce Readiness

## Syfte
Detta dokument beskriver vad som är aktivt i nuläget och vad som aktiveras när bankavtal för Swish Commerce är klart.

## Nuvarande läge
- `SWISH_MODE=mock` används i drift tills bankens aktivering är genomförd.
- Bokning och betalstatus testas med mock-flöde.
- Systemet lagrar endast bokningsstatus och Swish-referenser.

## Redo för aktivering
Följande stöd finns redan i koden:
- mTLS-konfiguration via `SWISH_CERT_PATH`, `SWISH_KEY_PATH`, `SWISH_CA_PATH`
- Swish API-bas via `SWISH_API_URL`
- Callback med sekretesskontroll i icke-mock-läge via `WEBHOOK_SECRET`
- Bakåtkompatibla variabelalias (`SWISH_COMMERCE_*`, `NOTIFY_WEBHOOK_SECRET`)

## Aktiveringschecklista
1. Sätt `SWISH_MODE=production`.
2. Sätt `SWISH_API_URL` till bankens Swish Commerce-endpoint.
3. Lägg in certifikat, nyckel och CA-sökvägar i miljövariabler.
4. Sätt `WEBHOOK_SECRET` (minst 32 slumpade tecken) och konfigurera samma värde i callback-avsändare.
5. Kör release-gate i `docs/RELEASE_GATE.md`.
6. Verifiera första testbetalning med spårbar referens.

## Spårbarhet
Vid varje produktionssläpp ska följande kunna visas:
- aktiv commit
- miljövariabler satta i hostingmiljö
- testresultat för kritiska betalflöden
