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

El comando mantiene solo los próximos 3 entrenamientos del plan, los agenda y los envía al último dispositivo Garmin. Es reanudable: `data/garmin_plan_state.json` evita repetir lo que ya terminó. Las sesiones de fuerza están limitadas a menos de 50 pasos expandidos, que es el límite relevante del reloj.

## Sincronización y correos diarios

A las 21:00 se sincronizan Garmin/Oura, se prepara el plan y se envía el entrenamiento de mañana. A las 12:00 se vuelven a sincronizar las fuentes y se envían el sueño y la actividad completa de ayer. Para el correo usa Resend. Crea una API key y agrega `RESEND_API_KEY`, `EMAIL_FROM` y `EMAIL_TO` al `.env`. `onboarding@resend.dev` sirve para pruebas; para producción debes verificar un dominio en Resend.

```bash
uv run python daily_job.py --training-email
uv run python daily_job.py --summary-email
```

Para instalar ambos horarios en macOS:

```bash
uv run python install_schedule.py
```

Los horarios usan la zona horaria del Mac. Configúralo en `America/Mexico_City`.
