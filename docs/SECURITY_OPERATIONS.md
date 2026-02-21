# Säkerhet och drift

## Säkerhetsprinciper
- Minsta möjliga lagring av kunddata.
- Inga hemligheter i repository.
- Admin- och dev-endpoints kräver token.
- Filuppladdning valideras (MIME, filändelse, storlek).
- Rate limiting används för rapportfunktion.

## Hemligheter och nyckelrotation
- Alla secrets hanteras i hostingplattformens miljövariabler.
- `WEBHOOK_SECRET` ska vara slumpad och minst 32 tecken i produktion.
- `WEBHOOK_SECRET` roteras vid misstanke om läckage.
- `ADMIN_TOKEN` roteras vid personalförändring eller misstänkt exponering.
- Certifikat/nycklar för Swish mTLS lagras endast i säker miljökonfiguration.

## Incidenthantering
1. Isolera incident: stäng dev-endpoints externt vid behov.
2. Rotera berörda hemligheter.
3. Verifiera loggar för betalstatus, webhookfel och adminanrop.
4. Dokumentera tidslinje och korrigerande åtgärd.

## Loggning och uppföljning
Följande händelser ska övervakas:
- misslyckade webhookleveranser
- upprepade rate-limit-träffar
- obehöriga admin-/dev-anrop
- avvikande betalstatusövergångar
- request-id (`X-Request-Id`) för spårning mellan klient och serverloggar

## Accesspolicy
- Endast behöriga administratörer får tillgång till Render-miljö och hemligheter.
- Delade tokens får inte användas i klientkod eller externa script utan behov.
