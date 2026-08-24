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

## Plan mensual y Garmin

El plan de cuatro semanas está en `plans/running-2026-08-25.json`. Usa zonas provisionales calculadas con FC de reposo de Oura y los máximos fiables de Strava.

La conexión directa con Garmin Connect usa una API no oficial. El primer login requiere contraseña y, si aplica, MFA; después conserva únicamente tokens locales en `data/garmin_tokens`.

```bash
# En .env: GARMIN_EMAIL=tu-correo
uv run python garmin_sync.py --push
```

El comando mantiene solo los próximos días del plan, los agenda y los envía al último dispositivo Garmin. Es reanudable: `data/garmin_plan_state.json` evita repetir lo que ya terminó.

## Sincronización y correo nocturno

El trabajo nocturno sincroniza Strava/Oura, revisa el entrenamiento ejecutado, actualiza el logro y prepara el siguiente entrenamiento. Para el correo usa Resend. Crea una API key y agrega `RESEND_API_KEY`, `EMAIL_FROM` y `EMAIL_TO` al `.env`. `onboarding@resend.dev` sirve para pruebas; para producción debes verificar un dominio en Resend.

```bash
uv run python daily_job.py --night
```

Para instalar dos ejecuciones diarias en macOS (mañana y 21:00):

```bash
uv run python install_schedule.py
```

La hora nocturna usa la zona horaria del Mac. Configúralo en `America/Mexico_City` para que sean las 21:00 CDMX.
