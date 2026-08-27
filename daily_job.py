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
from main import DB_PATH, PARAM, db, init_db, sync_oura, sync_strava

EXERCISE_NAMES = {
    "PUSH_UP": "lagartijas",
    "SCAPULAR_RETRACTION": "scapular pull-ups",
    "HANGING_KNEE_RAISE": "abdominales colgado",
    "PULL_UP": "pull-ups",
    "RING_ROW": "remo en aros",
    "BODY_WEIGHT_DIP": "dips asistidos con rango completo",
    "AIR_SQUAT": "sentadillas",
    "REVERSE_LUNGE_WITH_REACH_BACK": "zancadas hacia atrás por pierna",
    "SINGLE_LEG_HIP_RAISE": "puente de glúteo por pierna",
    "STANDING_CALF_RAISE": "elevaciones de pantorrilla",
    "DEAD_BUG": "dead bug por lado",
    "SIDE_PLANK": "plancha lateral por lado",
}


def records(source: str, kind: str) -> list[dict]:
    with db() as connection:
        return [
            json.loads(row["payload"] if isinstance(row, dict) else row[0])
            for row in connection.execute(
                f"SELECT payload FROM source_records WHERE source={PARAM} AND kind={PARAM}",
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
        if "meters" in step:
            duration = f"{step['meters']:g} m" if step["meters"] < 1000 else f"{step['meters'] / 1000:g} km"
        else:
            seconds = step["seconds"]
            duration = f"{seconds // 60:g} min" if seconds % 60 == 0 else f"{seconds:g} s"
        zone = step.get("zone")
        if zone:
            target = f"{zone} ({zones[zone][0]}–{zones[zone][1]} bpm)"
        else:
            target = step.get("effort", "libre")
        lines.append(f"{indent}- {step['type']}: {duration}, {target}")
    return lines


def training_summary(day: dict | None) -> str:
    if not day:
        return "No quedan entrenamientos en el bloque actual."
    heading = f"{day['date']} {day.get('time', '')} · {day['name']}".replace("  ·", " ·")
    if day["type"] != "strength":
        return "\n".join([heading, day["description"], *format_steps(day["steps"], day["zones"])])
    exercises = [
        f"- {sets}×{reps} {EXERCISE_NAMES.get(exercise, exercise.lower())}, 90 s descanso"
        for _, exercise, sets, reps in day["exercises"]
    ]
    return "\n".join([heading, day["description"], "Deja 1–2 repeticiones limpias en reserva:", *exercises])


def completed_session(planned: dict, day: str) -> bool:
    strava_types = {
        "run": {"Run", "VirtualRun"}, "bike": {"Ride", "VirtualRide", "MountainBikeRide"},
        "swim": {"Swim"},
    }
    garmin_types = {
        "run": {"running"}, "bike": {"cycling", "road_biking", "indoor_cycling"},
        "swim": {"lap_swimming", "open_water_swimming", "swimming"}, "strength": {"strength_training"},
    }
    return any(
        activity_day(item) == day and item.get("sport_type") in strava_types.get(planned["type"], set())
        for item in records("strava", "activity")
    ) or any(
        activity_day(item) == day
        and (item.get("activityType") or {}).get("typeKey") in garmin_types[planned["type"]]
        for item in records("garmin", "activity")
    )


def today_status(target_day: date | None = None) -> str:
    day = (target_day or date.today()).isoformat()
    planned = [item for item in load_workouts() if item["date"] == day]
    if not planned:
        return "No había una sesión programada."
    return "\n".join(
        f"✅ Completaste: {item['name']}." if completed_session(item, day)
        else f"⏸️ No aparece completado: {item['name']}."
        for item in planned
    )


def activity_day(item: dict) -> str:
    return (item.get("day") or item.get("start_date") or item.get("startTimeLocal")
            or item.get("startLocal") or item.get("start_datetime") or "")[:10]


def daily_activity_summary(target_day: date | None = None) -> str:
    today = (target_day or date.today()).isoformat()
    daily = [item for item in records("oura", "daily_activity") if item.get("day") == today]
    activity = max(daily, key=lambda item: item.get("timestamp", "")) if daily else None
    garmin_daily = [
        item for item in records("garmin", "daily_summary")
        if item.get("calendarDate") == today
    ]
    garmin_summary = max(garmin_daily, key=lambda item: item.get("_fetched_at", "")) if garmin_daily else None

    # Oura is primary. Garmin supplies today's partial summary and missing activities.
    seen = set()
    activities = []
    for item in records("oura", "workout"):
        if activity_day(item) != today:
            continue
        name = item.get("activity") or item.get("label") or "Actividad"
        kcal = item.get("calories")
        key = (name.lower(), round(float(kcal or 0)))
        seen.add(key)
        activities.append((name, kcal))
    for item in records("garmin", "activity"):
        if activity_day(item) != today:
            continue
        kind = item.get("activityType") or {}
        name = kind.get("typeKey") or item.get("activityName") or kind.get("displayName") or "Actividad"
        kcal = item.get("calories") or item.get("caloriesBurned")
        key = (name.lower(), round(float(kcal or 0)))
        if key not in seen:
            activities.append((name, kcal))
            seen.add(key)
    if activity:
        lines = [f"Pasos: {activity.get('steps', 0):,}"]
        if (score := activity.get("score")) is not None:
            lines.append(f"Score de actividad Oura: {score}")
        active_calories = activity.get("active_calories")
        target_calories = activity.get("target_calories")
        if active_calories is not None:
            target_text = f" / meta {target_calories:,.0f}" if isinstance(target_calories, (int, float)) else ""
            status = " · ✅ cumplida" if target_calories and active_calories >= target_calories else " · ⏸️ pendiente"
            lines.append(f"Calorías activas: {float(active_calories):,.0f}{target_text} kcal{status}")
        if (calories := activity.get("total_calories")) is not None:
            lines.append(f"Calorías totales: {float(calories):,.0f} kcal")
    elif garmin_summary:
        steps = garmin_summary.get("totalSteps", 0)
        target = garmin_summary.get("dailyStepGoal")
        target_text = f" / meta Garmin {target:,}" if isinstance(target, (int, float)) else ""
        lines = [f"Pasos al corte: {steps:,}{target_text} · {'✅ cumplida' if target and steps >= target else '⏸️ pendiente'}"]
        if (calories := garmin_summary.get("totalKilocalories")) is not None:
            lines.append(f"Calorías al corte: {float(calories):,.0f} kcal")
    else:
        lines = ["Garmin y Oura todavía no tienen el resumen de actividad de ese día."]
    lines.append("Actividades: " + (", ".join(
        f"{name}" + (f" ({float(kcal):,.0f} kcal)" if kcal is not None else "")
        for name, kcal in activities
    ) if activities else "ninguna registrada aparte de los pasos."))
    return "\n".join(lines)


def next_trainings(start_day: date | None = None) -> tuple[list[dict], bool]:
    start_day = start_day or date.today()
    future = [item for item in load_workouts() if date.fromisoformat(item["date"]) >= start_day]
    if not future:
        return [], False
    next_date = future[0]["date"]
    return [item for item in future if item["date"] == next_date], next_date == start_day.isoformat()


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
    unique = {}
    for item in records("strava", "activity"):
        if item.get("sport_type") not in ("Run", "VirtualRun") or not item.get("moving_time"):
            continue
        activity_date = datetime.fromisoformat(item["start_date"].replace("Z", "+00:00")).date()
        unique[(activity_date, round(item["distance"] / 100))] = (activity_date, item)
    for item in records("garmin", "activity"):
        if (item.get("activityType") or {}).get("typeKey") != "running" or not item.get("distance"):
            continue
        raw_day = activity_day(item)
        if not raw_day:
            continue
        activity_date = date.fromisoformat(raw_day)
        normalized = {"distance": item["distance"], "moving_time": item.get("movingDuration") or item.get("duration")}
        unique[(activity_date, round(item["distance"] / 100))] = (activity_date, normalized)
    runs = sorted(unique.values(), key=lambda pair: pair[0])
    if not runs:
        return "Primer objetivo: completar tres sesiones consistentes esta semana."
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


def send_email(report: str) -> None:
    api_key = os.getenv("RESEND_API_KEY")
    recipient = os.getenv("EMAIL_TO")
    sender = os.getenv("EMAIL_FROM", "onboarding@resend.dev")
    if not api_key or not recipient:
        raise RuntimeError("Configura RESEND_API_KEY, EMAIL_FROM y EMAIL_TO en .env")

    def card(emoji: str, title: str, content: str, color: str) -> str:
        return f'''<section style="background:{color};border-radius:16px;padding:20px 22px;margin:14px 0;">
          <div style="font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#53606b;">{emoji} {html.escape(title)}</div>
          <div style="font-size:16px;line-height:1.65;white-space:pre-line;margin-top:8px;color:#18212b;">{html.escape(content)}</div>
        </section>'''

    if report == "training":
        workouts, is_tomorrow = next_trainings(date.today() + timedelta(days=1))
        heading = ("Entrenamiento" if len(workouts) == 1 else "Entrenamientos") + " de mañana" if is_tomorrow else "Mañana toca descanso; próximo entrenamiento"
        workout_texts = [training_summary(workout) for workout in workouts]
        subject = heading
        eyebrow = "Preparación nocturna"
        intro = "Tu plan ya está listo para mañana."
        icons = {"run": "🏃‍♂️", "bike": "🚴", "swim": "🏊", "strength": "💪"}
        sections = [
            card(icons[workout["type"]], workout["name"], text, '#fff5df')
            for workout, text in zip(workouts, workout_texts)
        ] or [card("😴", heading, "Descanso programado.", "#eef4ff")]
        body_parts = [heading, *(workout_texts or ["Descanso programado."])]
    else:
        yesterday = date.today() - timedelta(days=1)
        sleep, sleep_advice = sleep_summary()
        status_text = today_status(yesterday)
        activity_text = daily_activity_summary(yesterday)
        achievement_text = achievement()
        subject = "Sueño y resumen de ayer"
        eyebrow = "Resumen diario"
        intro = "Tu sueño y la actividad completa de ayer."
        sections = [
            card('🌙', 'Cómo dormiste', sleep, '#eef8f4'),
            card('🛌', 'Cómo dormir hoy', sleep_advice, '#f4f0ff'),
            card('📊', 'Entrenamiento de ayer', status_text, '#eef4ff'),
            card('🚶', 'Actividad de ayer', activity_text, '#eef8f4'),
            card('🏆', 'Tu logro', achievement_text, '#eaf3ff'),
        ]
        body_parts = [f"Cómo dormiste\n{sleep}", f"Cómo dormir hoy\n{sleep_advice}",
                      f"Entrenamiento de ayer\n{status_text}", f"Actividad de ayer\n{activity_text}",
                      f"Logro\n{achievement_text}"]

    body = "\n\n".join([f"AI Trainer · {eyebrow}", "Hola, Rafa! 👋", *body_parts])
    html_body = f'''<div style="margin:0;background:#f4f7f8;padding:24px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#18212b;">
      <main style="max-width:620px;margin:auto;background:#ffffff;border-radius:22px;padding:26px;box-shadow:0 5px 24px #17212b14;">
        <div style="font-size:13px;color:#65727d;letter-spacing:.08em;text-transform:uppercase;">AI Trainer · {html.escape(eyebrow)}</div>
        <h1 style="font-size:28px;line-height:1.2;margin:10px 0 4px;">Hola, Rafa! 👋</h1>
        <p style="margin:0 0 18px;color:#65727d;font-size:15px;">{html.escape(intro)}</p>
        {''.join(sections)}
        <p style="font-size:13px;line-height:1.5;color:#65727d;margin:22px 4px 0;">Garmin mantiene automáticamente los entrenamientos de los próximos días.</p>
      </main>
    </div>'''
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [recipient], "subject": f"AI Trainer · {subject}", "html": html_body, "text": body},
        timeout=30,
    )
    response.raise_for_status()
    print(f"Resend email: {response.json().get('id', 'ok')}")


async def safe_sync(label: str, operation):
    try:
        return await operation()
    except Exception as error:
        return f"error: {label}: {error}"


async def sync_sources() -> None:
    strava, oura, garmin = await asyncio.gather(
        safe_sync("Strava", sync_strava),
        safe_sync("Oura", sync_oura),
        asyncio.to_thread(sync_garmin_activities),
        return_exceptions=True,
    )
    if isinstance(garmin, Exception):
        garmin = f"error: Garmin: {garmin}"
    print(f"Sincronización: Strava +{strava}, Oura +{oura}, Garmin +{garmin}")


if __name__ == "__main__":
    load_dotenv()
    init_db()
    parser = argparse.ArgumentParser()
    emails = parser.add_mutually_exclusive_group()
    emails.add_argument("--training-email", action="store_true", help="Envía el entrenamiento de mañana")
    emails.add_argument("--summary-email", action="store_true", help="Envía sueño y resumen de ayer")
    args = parser.parse_args()
    asyncio.run(sync_sources())
    if args.training_email:
        upload_plan(horizon_days=2)
        send_email("training")
        print("Correo de entrenamiento enviado")
    elif args.summary_email:
        send_email("summary")
        print("Correo de resumen enviado")
