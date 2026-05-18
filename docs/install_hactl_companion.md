# Task: hactl_companion HA Add-on per Supervisor-API installieren

Installiere das HA Add-on `hactl_companion` (Repo: `https://github.com/hemm-ems/hactl-companion`) auf einer Home-Assistant-Instanz **ohne SSH**, ausschließlich über die HTTP-API.

## Inputs vom User abfragen (falls nicht in env)
- `HA_URL` — z.B. `https://homeassistant.local:8123` (kein trailing slash)
- `HA_TOKEN` — Long-Lived Access Token aus HA-Profil (Admin-User)

## Vorbedingung prüfen
1. `GET {HA_URL}/api/` mit `Authorization: Bearer {HA_TOKEN}` → muss 200 + `{"message":"API running."}` liefern. Sonst Token/URL falsch → abbrechen.
2. `GET {HA_URL}/api/hassio/supervisor/info` → 200 erwartet. Bei 404 hat die Instanz keinen Supervisor (HA Container) → abbrechen mit Hinweis.

## Installation (3 Schritte)
Alle Calls mit Header: `Authorization: Bearer {HA_TOKEN}`, `Content-Type: application/json`.

1. **Repo hinzufügen**
   ```
   POST {HA_URL}/api/hassio/store/repositories
   Body: {"repository": "https://github.com/hemm-ems/hactl-companion"}
   ```
   Idempotent — bei "already exists" weitermachen.

2. **Slug ermitteln**
   ```
   GET {HA_URL}/api/hassio/store
   ```
   Im Response unter `data.addons[]` das Add-on finden, dessen `repository`-URL dem Repo entspricht. Dessen `slug` merken.
   Falls leer: einmal `POST {HA_URL}/api/hassio/store/reload`, dann nochmal lesen.

3. **Installieren + starten**
   ```
   POST {HA_URL}/api/hassio/store/addons/{slug}/install
   POST {HA_URL}/api/hassio/addons/{slug}/start
   ```
   Install kann 1–5 Minuten dauern (Docker-Build). Polling:
   ```
   GET {HA_URL}/api/hassio/addons/{slug}/info
   ```
   Auf `data.version` ≠ null und `data.state` warten.

## Fehlerbehandlung
- **401/403** → Token ungültig oder kein Admin → abbrechen, User informieren.
- **Install hängt** → Supervisor-Logs holen: `GET {HA_URL}/api/hassio/supervisor/logs` (text/plain). Letzte 50 Zeilen ausgeben.
- **Install schlägt fehl** → Einmal `POST {HA_URL}/api/hassio/supervisor/restart`, dann Install erneut versuchen (max. 1 Retry).

## Output
Am Ende ausgeben: Slug, Version, State, Ingress-URL (`data.ingress_url` aus info-Endpoint).

## Hinweise
- Keine SSH-Calls, keine File-Edits auf dem HA-Host.
- Repo ist early-stage — wenn Install fehlschlägt, nicht reparieren versuchen, nur Logs zurückgeben.
- Token niemals loggen oder in Output schreiben.
