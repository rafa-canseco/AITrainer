# Trainer

Importa datos de Strava, Garmin y Oura para analizar el entrenamiento y generar rutinas diarias.

## Desarrollo

```bash
uv sync
cp .env.example .env
uv run uvicorn main:app --reload --env-file .env
```

Crea las aplicaciones OAuth aquí:

- Strava: <https://www.strava.com/settings/api>
- Oura: <https://developer.ouraring.com/applications>

Configura los callbacks exactamente así:

```text
http://localhost:8000/oauth/strava/callback
http://localhost:8000/oauth/oura/callback
```

Después abre `/connect/strava` y `/connect/oura` en el navegador. No abras directamente `/oauth/.../callback`: esa URL solo funciona después de autorizar y recibir el parámetro `code`.

La API queda en <http://localhost:8000> y crea la base SQLite local en `data/trainer.db`.

Para cambiar la ruta de la base:

```bash
TRAINER_DB_PATH=/otra/ruta/trainer.db uv run uvicorn main:app --reload
```
