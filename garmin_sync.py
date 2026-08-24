import argparse
import getpass
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin

PLAN_PATH = Path("plans/running-2026-08-25.json")
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
    value = step.get("meters", step.get("seconds"))
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
        "endConditionValue": value,
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


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def upload_plan(push: bool) -> None:
    plan = json.loads(PLAN_PATH.read_text())
    zones = plan["zones"]
    runs = [day for week in plan["weeks"] for day in week["days"] if day["type"] == "run"]
    client = garmin_client()
    state = load_state()

    for day in runs:
        key = f"{day['date']} {day['name']}"
        item = state.setdefault(key, {})
        if "workout_id" not in item:
            steps = workout_steps(day["steps"], zones)
            payload = {
                "workoutName": day["name"],
                "description": day["description"],
                "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                "estimatedDurationInSecs": estimated_seconds(day["steps"]),
                "workoutSegments": [{
                    "segmentOrder": 1,
                    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                    "workoutSteps": steps,
                }],
            }
            uploaded = client.upload_workout(payload)
            item["workout_id"] = uploaded["workoutId"]
            save_state(state)

        if not item.get("scheduled"):
            client.schedule_workout(item["workout_id"], day["date"])
            item["scheduled"] = True
            save_state(state)

        if push and not item.get("pushed"):
            client.push_workout_to_device(item["workout_id"])
            item["pushed"] = True
            save_state(state)

        print(f"OK {day['date']} {day['name']} (Garmin {item['workout_id']})")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Sube y agenda el plan de carrera en Garmin Connect")
    parser.add_argument("--push", action="store_true", help="Además envía cada workout al último dispositivo Garmin")
    args = parser.parse_args()
    upload_plan(args.push)
