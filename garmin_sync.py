import argparse
import getpass
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin
from garminconnect.exercises import EXERCISES
from garminconnect.workout import StrengthWorkout, WorkoutSegment, create_strength_set

ROOT = Path(__file__).resolve().parent
PLAN_PATH = ROOT / "plans/olympic-foundation-2026-08-27.json"
TOKEN_PATH = ROOT / "data/garmin_tokens"
STATE_PATH = ROOT / "data/garmin_plan_state.json"


def garmin_client() -> Garmin:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token_json = os.getenv("GARMIN_TOKENS")
    if token_json:
        client = Garmin()
        client.login(token_json)
        return client
    if TOKEN_PATH.exists():
        client = Garmin()
    else:
        email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ")
        password = getpass.getpass("Garmin password: ")
        client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: "))
    client.login(str(TOKEN_PATH))
    return client


def target(zone: str | None, zones: dict[str, list[int]]) -> dict:
    if not zone:
        return {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}}
    low, high = zones[zone]
    return {
        "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
        "targetValueOne": low,
        "targetValueTwo": high,
    }


def executable(step: dict, order: int, zones: dict[str, list[int]]) -> dict:
    condition = "distance" if "meters" in step else "time"
    result = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {
            "stepTypeId": {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4}[step["type"]],
            "stepTypeKey": step["type"],
        },
        "endCondition": {
            "conditionTypeId": 3 if condition == "distance" else 2,
            "conditionTypeKey": condition,
        },
        "endConditionValue": step.get("meters", step.get("seconds")),
    }
    result.update(target(step.get("zone"), zones))
    return result


def workout_steps(steps: list[dict], zones: dict[str, list[int]]) -> list[dict]:
    result = []
    for order, step in enumerate(steps, 1):
        if step["type"] == "repeat":
            result.append({
                "type": "RepeatGroupDTO",
                "stepOrder": order,
                "numberOfIterations": step["times"],
                "workoutSteps": [
                    executable(child, child_order, zones)
                    for child_order, child in enumerate(step["steps"], 1)
                ],
            })
        else:
            result.append(executable(step, order, zones))
    return result


def estimated_seconds(steps: list[dict]) -> int:
    total = 0
    for step in steps:
        if step["type"] == "repeat":
            total += step["times"] * estimated_seconds(step["steps"])
        else:
            total += step.get("seconds", step.get("meters", 0) * 0.54)
    return round(total)


def sync_garmin_activities(days: int = 3) -> int:
    from main import save_records

    client = garmin_client()
    end = date.today()
    start = end - timedelta(days=days)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    summaries = []
    for summary_day in (end - timedelta(days=1), end):
        summary = client.get_user_summary(summary_day.isoformat())
        summary["_fetched_at"] = datetime.now().astimezone().isoformat()
        summaries.append(summary)
    return (save_records("garmin", "activity", activities)
            + save_records("garmin", "daily_summary", summaries))


def load_workouts() -> list[dict]:
    plan = json.loads(PLAN_PATH.read_text())
    workouts = [{**item, "zones": plan["run_zones"]} for item in plan["workouts"]]
    supported = {"run", "bike", "swim", "strength"}
    ids = [item["id"] for item in workouts]
    if len(ids) != len(set(ids)) or any(item["type"] not in supported for item in workouts):
        raise ValueError("El plan tiene IDs duplicados o tipos no soportados")
    valid_exercises = {(item["category"], item["exercise"]) for item in EXERCISES}
    for item in workouts:
        date.fromisoformat(item["date"])
        if any((category, exercise) not in valid_exercises
               for category, exercise, *_ in item.get("exercises", [])):
            raise ValueError(f"Ejercicio Garmin no soportado: {item['name']}")
    return sorted(workouts, key=lambda item: (item["date"], item.get("time", "")))


SPORTS = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running"},
    "bike": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    "swim": {"sportTypeId": 4, "sportTypeKey": "swimming"},
}


def endurance_payload(day: dict) -> dict:
    sport = SPORTS[day["type"]]
    return {
        "workoutName": day["name"],
        "description": day["description"],
        "sportType": sport,
        "estimatedDurationInSecs": day.get("estimated_seconds", estimated_seconds(day["steps"])),
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": sport,
            "workoutSteps": workout_steps(day["steps"], day["zones"]),
        }],
    }


def strength_payload(day: dict) -> dict:
    steps = []
    order = 1
    for category, exercise, sets, reps in day["exercises"]:
        steps.append(create_strength_set(category, order, sets, reps, 90, exercise))
        order += 3
    return StrengthWorkout(
        workoutName=day["name"],
        description=day["description"],
        estimatedDurationInSecs=day.get("estimated_seconds", 2100),
        workoutSegments=[WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 5, "sportTypeKey": "strength_training"},
            workoutSteps=steps,
        )],
    ).to_dict()


def workout_payload(day: dict) -> dict:
    return strength_payload(day) if day["type"] == "strength" else endurance_payload(day)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def load_state() -> dict:
    from main import db, init_db, PARAM

    init_db()
    with db() as connection:
        rows = connection.execute("SELECT state_key, payload FROM plan_state").fetchall()
    if rows:
        return {row["state_key"] if isinstance(row, dict) else row[0]: json.loads(row["payload"] if isinstance(row, dict) else row[1]) for row in rows}
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    from main import db, init_db, PARAM

    init_db()
    with db() as connection:
        connection.execute("DELETE FROM plan_state")
        for key, payload in state.items():
            connection.execute(
                f"INSERT INTO plan_state(state_key, payload) VALUES ({PARAM}, {PARAM})",
                (key, json.dumps(payload)),
            )
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def delete_tracked(
    client: Garmin, state: dict, *, all_workouts: bool = False, valid_ids: set[str] | None = None,
) -> None:
    cutoff = date.today().isoformat()
    for key, item in list(state.items()):
        stale = valid_ids is not None and key not in valid_ids
        if not all_workouts and not stale and item.get("date", "") >= cutoff:
            continue
        try:
            client.delete_workout(item["workout_id"])
        except Exception as error:
            if "404" not in str(error):
                print(f"AVISO al borrar {key}: {error}")
                continue
        print(f"BORRADO {key}")
        state.pop(key)
        save_state(state)


def upload_plan(push: bool = False, horizon_days: int = 2, replace: bool = False) -> None:
    workouts = load_workouts()
    client = garmin_client()
    state = load_state()
    delete_tracked(client, state, all_workouts=replace, valid_ids={item["id"] for item in workouts})

    start = date.today()
    end = start + timedelta(days=horizon_days)
    for day in workouts:
        if not start <= date.fromisoformat(day["date"]) <= end:
            continue
        key = day["id"]
        payload = workout_payload(day)
        digest = payload_hash(payload)
        item = state.get(key)
        if item and item.get("payload_hash") != digest:
            try:
                client.delete_workout(item["workout_id"])
            except Exception as error:
                if "404" not in str(error):
                    raise
            state.pop(key)
            item = None
            save_state(state)
        if not item:
            uploaded = client.upload_workout(payload)
            item = state[key] = {
                "date": day["date"], "type": day["type"],
                "workout_id": uploaded["workoutId"], "payload_hash": digest,
            }
            save_state(state)

        if not item.get("scheduled"):
            client.schedule_workout(item["workout_id"], day["date"])
            item["scheduled"] = True
            save_state(state)

        if push and not item.get("pushed"):
            try:
                client.push_workout_to_device(item["workout_id"])
                item["pushed"] = True
                item.pop("push_error", None)
            except Exception as error:
                item["push_error"] = str(error)
                print(f"AVISO: no se pudo empujar al reloj; queda agendado: {error}")
            save_state(state)

        print(f"OK {day['date']} {day['name']} (Garmin {item['workout_id']})")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Mantiene una ventana móvil del plan en Garmin Connect")
    parser.add_argument("--days", type=int, default=2, help="Días futuros que se mantienen en Garmin")
    parser.add_argument("--push", action="store_true", help="También envía cada workout al último dispositivo")
    parser.add_argument("--replace", action="store_true", help="Borra los workouts administrados y vuelve a crear la ventana")
    args = parser.parse_args()
    upload_plan(args.push, args.days, args.replace)
