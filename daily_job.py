import argparse
import asyncio
import html
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv

from garmin_sync import load_workouts, sync_garmin_activities, upload_plan
from main import DB_PATH, init_db, sync_oura, sync_strava

EXERCISE_NAMES = {
    "PUSH_UP": "lagartijas",
    "SCAPULAR_RETRACTION": "scapular pull-ups",
    "HANGING_KNEE_RAISE": "abdominales colgado",
    "PULL_UP": "pull-ups",
    "RING_ROW": "remo en aros",
    "BODY_WEIGHT_DIP": "dips",
}


def records(source: str, kind: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM source_records WHERE source=? AND kind=?",
                (source, kind),
            )
        ]


def sleep_summary() -> tuple[str, str]:
    sleeps = [item for item in records("oura", "sleep") if item.get("type") == "long_sleep"]
    latest = max(sleeps, key=lambda item: item["day"])
    scores = {item["day"]: item for item in records("oura", "daily_sleep")}
    score = scores.get(latest["day"], {}).get("score", "sin score")
    hours = latest["total_sleep_duration"] / 3600
    deep = latest.get("deep_sleep_duration", 0) // 60
    rem = latest.get("rem_sleep_duration", 0) // 60
    summary = (
        f"Noche del {latest['day']}: {hours:.1f} h de sueño, score {score}, "
        f"eficiencia {latest.get('efficiency', 'N/D')}%, profundo {deep:.0f} min, "
        f"REM {rem:.0f} min, HRV {latest.get('average_hrv', 'N/D')} ms y "
        f"FC mínima {latest.get('lowest_heart_rate', 'N/D')} bpm."
    )
    advice = "Busca 8–8.5 h en cama y termina cafeína al menos 8 h antes de dormir."
    if hours < 7:
        advice += " Vienes corto de sueño: acuéstate 60–90 min antes y mantén mañana fácil si despiertas pesado."
    elif latest.get("efficiency", 100) < 85:
        advice += " Tu eficiencia fue baja: reduce pantallas y comida pesada durante la última hora."
    else:
        advice += " La última noche fue suficiente; repite horario y rutina."
    return summary, advice


def format_steps(steps: list[dict], zones: dict[str, list[int]], indent: str = "") -> list[str]:
    lines = []
    for step in steps:
        if step["type"] == "repeat":
            lines.append(f"{indent}{step['times']} rondas:")
            lines.extend(format_steps(step["steps"], zones, indent + "  "))
            continue
        duration = f"{step['meters'] / 1000:g} km" if "meters" in step else f"{step['seconds'] // 60:g} min"
        zone = step.get("zone")
        target = f" {zone} ({zones[zone][0]}–{zones[zone][1]} bpm)" if zone else " libre"
        lines.append(f"{indent}- {step['type']}: {duration},{target}")
    return lines


def training_summary(day: dict | None) -> str:
    if not day:
        return "No quedan entrenamientos en el bloque actual."
    if day["type"] == "run":
        return "\n".join([
            f"{day['date']} · {day['name']}",
            day["description"],
            *format_steps(day["steps"], day["zones"]),
        ])
    exercises = [
        f"- {sets}×{reps} {EXERCISE_NAMES.get(exercise, exercise.lower())}, 90 s descanso"
        for _, exercise, sets, reps in day["exercises"]
    ]
    return "\n".join([
        f"{day['date']} · {day['name']} · {day['focus']}",
        "Calienta hombros; luego 3 rondas de dead hang 30 s + 10 lagartijas.",
        "Deja 1 repetición en reserva:",
        *exercises,
    ])


def next_training() -> tuple[dict | None, bool]:
    tomorrow = date.today() + timedelta(days=1)
    future = [item for item in load_workouts() if date.fromisoformat(item["date"]) >= tomorrow]
    if not future:
        return None, False
    return future[0], future[0]["date"] == tomorrow.isoformat()


def training_streak() -> int:
    days = set()
    for source, kind in (("strava", "activity"), ("garmin", "activity")):
        for item in records(source, kind):
            raw = item.get("start_date") or item.get("startLocal") or item.get("startTimeLocal")
            if raw:
                days.add(raw[:10])
    streak = 0
    cursor = date.today()
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def achievement() -> str:
    streak = training_streak()
    runs = []
    for item in records("strava", "activity"):
        if item.get("sport_type") not in ("Run", "VirtualRun") or not item.get("moving_time"):
            continue
        activity_date = datetime.fromisoformat(item["start_date"].replace("Z", "+00:00")).date()
        runs.append((activity_date, item))
    if not runs:
        return "Primer objetivo: completar tres sesiones consistentes esta semana."
    runs.sort(key=lambda pair: pair[0])
    latest_date, latest = runs[-1]
    latest_distance = latest["distance"] / 1000
    latest_pace = latest["moving_time"] / latest_distance / 60
    comparable = [
        item for activity_date, item in runs[:-1]
        if latest_date - timedelta(days=60) <= activity_date < latest_date
        and 0.8 * latest["distance"] <= item.get("distance", 0) <= 1.2 * latest["distance"]
    ]
    if comparable:
        baseline = sum(item["moving_time"] / (item["distance"] / 1000) / 60 for item in comparable) / len(comparable)
        if latest_pace < baseline * 0.97:
            improvement = round((baseline - latest_pace) * 60)
            return f"Logro: tu última carrera fue {improvement} s/km más rápida que tus carreras comparables. Racha actual: {streak} día(s)."
    week_count = sum(activity_date >= date.today() - timedelta(days=7) for activity_date, _ in runs)
    return f"Progreso: completaste {week_count} carrera(s) en los últimos 7 días. Racha actual: {streak} día(s). La siguiente meta es sostener 3 por semana."


def send_email() -> None:
    api_key = os.getenv("RESEND_API_KEY")
    recipient = os.getenv("EMAIL_TO")
    sender = os.getenv("EMAIL_FROM", "onboarding@resend.dev")
    if not api_key or not recipient:
        raise RuntimeError("Configura RESEND_API_KEY, EMAIL_FROM y EMAIL_TO en .env")
    sleep, sleep_advice = sleep_summary()
    workout, is_tomorrow = next_training()
    heading = "Entrenamiento de mañana" if is_tomorrow else "Mañana toca descanso; próximo entrenamiento"
    workout_text = training_summary(workout)
    achievement_text = achievement()
    body = "\n\n".join([
        "AI Trainer · Preparación nocturna",
        f"Hola, Rafa! 👋\n\nCómo dormiste\n{sleep}",
        f"Cómo dormir hoy\n{sleep_advice}",
        f"{heading}\n{workout_text}",
        f"Logro\n{achievement_text}",
        "Recuerda sincronizar tu Forerunner 965 esta noche para recibir el entrenamiento.",
        "Garmin mantendrá automáticamente solo los próximos 3 entrenamientos.",
    ])
    def card(emoji: str, title: str, content: str, color: str) -> str:
        return f'''<section style="background:{color};border-radius:16px;padding:20px 22px;margin:14px 0;">
          <div style="font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#53606b;">{emoji} {html.escape(title)}</div>
          <div style="font-size:16px;line-height:1.65;white-space:pre-line;margin-top:8px;color:#18212b;">{html.escape(content)}</div>
        </section>'''
    html_body = f'''<div style="margin:0;background:#f4f7f8;padding:24px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#18212b;">
      <main style="max-width:620px;margin:auto;background:#ffffff;border-radius:22px;padding:26px;box-shadow:0 5px 24px #17212b14;">
        <div style="font-size:13px;color:#65727d;letter-spacing:.08em;text-transform:uppercase;">AI Trainer · preparación nocturna</div>
        <h1 style="font-size:28px;line-height:1.2;margin:10px 0 4px;">Hola, Rafa! 👋</h1>
        <p style="margin:0 0 18px;color:#65727d;font-size:15px;">Un vistazo a tu recuperación y a lo que toca mañana.</p>
        {card('🌙', 'Cómo dormiste', sleep, '#eef8f4')}
        {card('🛌', 'Cómo dormir hoy', sleep_advice, '#f4f0ff')}
        {card('🏃‍♂️' if workout and workout['type'] == 'run' else '💪', heading, workout_text, '#fff5df')}
        {card('🏆', 'Tu logro', achievement_text, '#eaf3ff')}
        {card('⌚', 'Recordatorio', 'Sincroniza tu Forerunner 965 esta noche para recibir el entrenamiento de mañana.', '#fff0f0')}
        <p style="font-size:13px;line-height:1.5;color:#65727d;margin:22px 4px 0;">Tu plan se sincroniza automáticamente y Garmin conserva solo los próximos 3 entrenamientos.</p>
      </main>
    </div>'''
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [recipient], "subject": f"AI Trainer · {heading}", "html": html_body, "text": body},
        timeout=30,
    )
    response.raise_for_status()
    print(f"Resend email: {response.json().get('id', 'ok')}")


async def sync_sources() -> None:
    strava, oura = await asyncio.gather(sync_strava(), sync_oura())
    try:
        garmin = await asyncio.to_thread(sync_garmin_activities)
    except Exception as error:
        garmin = f"error: {error}"
    print(f"Sincronización: Strava +{strava}, Oura +{oura}, Garmin +{garmin}")


if __name__ == "__main__":
    load_dotenv()
    init_db()
    parser = argparse.ArgumentParser()
    parser.add_argument("--night", action="store_true", help="Mantiene Garmin y envía el correo nocturno")
    args = parser.parse_args()
    asyncio.run(sync_sources())
    if args.night:
        upload_plan(horizon_days=2)
        send_email()
        print("Correo nocturno enviado")
