# Release gate

## Kritiska flöden
Följande måste vara grönt före produktion:
- Bokning skapas med `PENDING_PAYMENT`.
- Swish payment request skapas utan fel.
- Callback uppdaterar status till `CONFIRMED` vid `PAID`.
- Kvitto-webhook skickas en gång (idempotent beteende).
- Misslyckad betalning leder till `CANCELLED`.
- Skaderapport accepterar giltiga bilder och nekar ogiltiga filer.

## Testkommando
```bash
pytest -q
```

## Manuell verifiering
- Kontrollera att `SWISH_MODE` är korrekt för miljön.
- Kontrollera att obligatoriska secrets finns satta.
- Kontrollera `/api/version` för spårbar commit.

## Stop-kriterier
Sätt release till stopp om:
- något test i kritiska flöden fallerar
- webhookhemlighet saknas i produktion
- admin-token saknas i produktion
