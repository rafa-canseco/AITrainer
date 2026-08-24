import argparse
import getpass
import json
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin
from garminconnect.workout import StrengthWorkout, WorkoutSegment, create_strength_set

RUN_PLAN_PATH = Path("plans/running-2026-08-25.json")
STRENGTH_PLAN_PATH = Path("plans/bodyweight-2026-08-26.json")
TOKEN_PATH = Path("data/garmin_tokens")
STATE_PATH = Path("data/garmin_plan_state.json")


def garmin_client() -> Garmin:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
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


def load_workouts() -> list[dict]:
    running = json.loads(RUN_PLAN_PATH.read_text())
    strength = json.loads(STRENGTH_PLAN_PATH.read_text())
    runs = [
        {**day, "zones": running["zones"]}
        for week in running["weeks"]
        for day in week["days"]
        if day["type"] == "run"
    ]
    return sorted(runs + strength["workouts"], key=lambda item: item["date"])


def running_payload(day: dict) -> dict:
    return {
        "workoutName": day["name"],
        "description": day["description"],
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "estimatedDurationInSecs": estimated_seconds(day["steps"]),
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
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
        description=f"{day['focus']}. Calienta hombros; 3 rondas de dead hang 30 s + 10 lagartijas. Deja 1 repetición en reserva.",
        estimatedDurationInSecs=2700,
        workoutSegments=[WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 5, "sportTypeKey": "strength_training"},
            workoutSteps=steps,
        )],
    ).to_dict()


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def delete_tracked(client: Garmin, state: dict, *, all_workouts: bool = False) -> None:
    cutoff = date.today().isoformat()
    for key, item in list(state.items()):
        if not all_workouts and item.get("date", key[:10]) >= cutoff:
            continue
        try:
            client.delete_workout(item["workout_id"])
        except Exception as error:
            print(f"AVISO al borrar {key}: {error}")
        else:
            print(f"BORRADO {key}")
        state.pop(key)
        save_state(state)


def upload_plan(push: bool = False, horizon_days: int = 3, replace: bool = False) -> None:
    client = garmin_client()
    state = load_state()
    if replace:
        delete_tracked(client, state, all_workouts=True)
    else:
        delete_tracked(client, state)

    start = date.today()
    end = start + timedelta(days=horizon_days)
    workouts = [day for day in load_workouts() if start <= date.fromisoformat(day["date"]) <= end]

    for day in workouts:
        key = f"{day['date']} {day['name']}"
        item = state.setdefault(key, {"date": day["date"], "type": day["type"]})
        if "workout_id" not in item:
            payload = running_payload(day) if day["type"] == "run" else strength_payload(day)
            uploaded = client.upload_workout(payload)
            item["workout_id"] = uploaded["workoutId"]
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
    parser.add_argument("--days", type=int, default=3, help="Días futuros que se mantienen en Garmin")
    parser.add_argument("--push", action="store_true", help="También envía cada workout al último dispositivo")
    parser.add_argument("--replace", action="store_true", help="Borra los workouts administrados y vuelve a crear la ventana")
    args = parser.parse_args()
    upload_plan(args.push, args.days, args.replace)
