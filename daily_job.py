import argparse
import asyncio
import json
import os
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from dotenv import load_dotenv

from garmin_sync import load_workouts, upload_plan
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


def achievement() -> str:
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
            return f"Logro: tu última carrera fue {improvement} s/km más rápida que tus carreras comparables de los 60 días previos."
    week_count = sum(activity_date >= date.today() - timedelta(days=7) for activity_date, _ in runs)
    return f"Progreso: completaste {week_count} carrera(s) en los últimos 7 días. La siguiente meta es sostener 3 por semana."


def send_email() -> None:
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("EMAIL_TO") or user
    if not user or not password or not recipient:
        raise RuntimeError("Configura GMAIL_USER, GMAIL_APP_PASSWORD y EMAIL_TO en .env")
    sleep, sleep_advice = sleep_summary()
    workout, is_tomorrow = next_training()
    heading = "Entrenamiento de mañana" if is_tomorrow else "Mañana toca descanso; próximo entrenamiento"
    body = "\n\n".join([
        "AI Trainer · Preparación nocturna",
        f"CÓMO DORMISTE\n{sleep}",
        f"CÓMO DORMIR HOY\n{sleep_advice}",
        f"{heading.upper()}\n{training_summary(workout)}",
        f"LOGRO\n{achievement()}",
        "Tu Garmin mantendrá automáticamente solo los próximos 7 días del plan.",
    ])
    message = EmailMessage()
    message["Subject"] = f"AI Trainer · {heading}"
    message["From"] = user
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password.replace(" ", ""))
        smtp.send_message(message)


async def sync_sources() -> None:
    strava, oura = await asyncio.gather(sync_strava(), sync_oura())
    print(f"Sincronización: Strava +{strava}, Oura +{oura}")


if __name__ == "__main__":
    load_dotenv()
    init_db()
    parser = argparse.ArgumentParser()
    parser.add_argument("--night", action="store_true", help="Mantiene Garmin y envía el correo nocturno")
    args = parser.parse_args()
    asyncio.run(sync_sources())
    if args.night:
        upload_plan(horizon_days=7)
        send_email()
        print("Correo nocturno enviado")
