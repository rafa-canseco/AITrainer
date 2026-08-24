# Trainer

Importa datos de Strava, Garmin y Oura para analizar el entrenamiento y generar rutinas diarias.

## Desarrollo

```bash
uv sync
uv run uvicorn main:app --reload
```

La API queda en <http://127.0.0.1:8000> y crea la base SQLite local en `data/trainer.db`.

Para cambiar la ruta de la base:

```bash
TRAINER_DB_PATH=/otra/ruta/trainer.db uv run uvicorn main:app --reload
```
