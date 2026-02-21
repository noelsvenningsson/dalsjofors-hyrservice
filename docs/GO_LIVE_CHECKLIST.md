# Go-live-checklista (utan Swish produktion)

## Syfte
Denna lista används för att avgöra om webbplatsen är redo att användas skarpt innan bankens Swish-aktivering är klar.

## Måste vara klart
- [x] Bokning fungerar från start till bekräftelse i test/mock-läge.
- [x] Admin-skydd finns på känsliga endpoints (`ADMIN_TOKEN`).
- [x] Skaderapportering fungerar med filvalidering och rate limiting.
- [x] Kvittoflöde via webhook fungerar med hemlighet (`WEBHOOK_SECRET`).
- [x] Backup-rutin finns och är dokumenterad (`./scripts/backup_db.sh`).
- [x] Full testsvit passerar.
- [x] Drift- och säkerhetsdokument finns för incidenter och nyckelrotation.

## Rekommenderat före bred lansering
- [ ] Sätt upp larm/övervakning för webhookfel och obehöriga adminanrop.
- [ ] Kör ett dokumenterat restore-test från backup i driftmiljö.
- [ ] Gör en kort intern rutin för kundsupport (hur bokning hittas via referens).
- [ ] Kör manuell mobiltest (iOS/Android) av hela bokningsflödet.

## Swish produktion (aktiveras när banken är klar)
- [ ] Sätt `SWISH_MODE=production`.
- [ ] Sätt `SWISH_API_URL` från bankens uppgifter.
- [ ] Konfigurera `SWISH_CERT_PATH`, `SWISH_KEY_PATH`, `SWISH_CA_PATH`.
- [ ] Verifiera callbackflöde med `WEBHOOK_SECRET`.
- [ ] Verifiera första riktiga betalningen med spårbar referens.
