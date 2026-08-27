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

## Plan olímpico y Garmin

El bloque inicial de ocho semanas está en `plans/olympic-foundation-2026-08-27.json`. Combina carrera, natación, bicicleta y dos sesiones full-body con prioridad en pull-ups. Los lunes son descanso completo. Las zonas de carrera son provisionales hasta los tests de umbral.

La conexión directa con Garmin Connect usa una API no oficial. El primer login requiere contraseña y, si aplica, MFA; después conserva únicamente tokens locales en `data/garmin_tokens`.

```bash
# En .env: GARMIN_EMAIL=tu-correo
uv run python garmin_sync.py --push
```

El comando mantiene una ventana móvil de los próximos días, agenda todas las sesiones y puede enviarlas al último dispositivo Garmin. Es reanudable: `data/garmin_plan_state.json` evita repetir lo que ya terminó y reemplaza un workout administrado cuando cambia su contenido.

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
