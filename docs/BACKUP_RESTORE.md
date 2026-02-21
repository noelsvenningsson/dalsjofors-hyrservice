# Backup och restore

## Backup
Kör:

```bash
scripts/backup_db.sh
```

Scriptet använder `DATABASE_PATH` om satt, annars `database.db` i projektroten.

## Restore
1. Stoppa applikationen.
2. Säkerhetskopiera aktuell databas.
3. Kopiera vald backup till aktiv `DATABASE_PATH`.
4. Starta applikationen.
5. Verifiera med hälsokontroll och ett lästest av bokningsdata.

## Rekommenderad rutin
- Daglig backup i produktion.
- Minst ett restore-test per månad.
- Dokumentera restore-tid och resultat.
