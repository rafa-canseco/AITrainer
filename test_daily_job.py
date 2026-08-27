import os
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import daily_job
import garmin_sync


class GarminRunFallbackTest(unittest.TestCase):
    def records(self, source, kind):
        today = date.today().isoformat()
        if (source, kind) == ("garmin", "activity"):
            return [{
                "activityName": "Intervalos",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": f"{today} 14:40:09",
                "calories": 459,
            }]
        if (source, kind) == ("garmin", "daily_summary"):
            return [{
                "calendarDate": today,
                "_fetched_at": f"{today}T21:00:00-06:00",
                "totalSteps": 7655,
                "dailyStepGoal": 10000,
                "totalKilocalories": 2218,
            }]
        return []

    @patch.object(daily_job, "load_workouts")
    @patch.object(daily_job, "records")
    def test_garmin_run_counts_without_strava_or_oura(self, records, load_workouts):
        records.side_effect = self.records
        load_workouts.return_value = [{
            "date": date.today().isoformat(),
            "type": "run",
            "name": "Intervalos",
        }]

        self.assertIn("✅ Completaste", daily_job.today_status())
        summary = daily_job.daily_activity_summary()
        self.assertIn("Pasos al corte: 7,655 / meta Garmin 10,000", summary)
        self.assertIn("Calorías al corte: 2,218 kcal", summary)
        self.assertIn("running (459 kcal)", summary)
        self.assertNotIn("\\n", summary)

    @patch.object(daily_job, "records")
    def test_oura_activity_uses_calorie_goal_not_meter_target(self, records):
        today = date.today().isoformat()
        records.side_effect = lambda source, kind: [{
            "day": today,
            "steps": 2797,
            "score": 78,
            "active_calories": 480,
            "target_calories": 650,
            "target_meters": 12000,
            "total_calories": 2896,
        }] if (source, kind) == ("oura", "daily_activity") else []

        summary = daily_job.daily_activity_summary()

        self.assertIn("Pasos: 2,797", summary)
        self.assertIn("Score de actividad Oura: 78", summary)
        self.assertIn("Calorías activas: 480 / meta 650 kcal", summary)
        self.assertNotIn("12,000", summary)

    @patch.object(daily_job, "next_trainings")
    @patch.object(daily_job.httpx, "post")
    def test_training_email_only_contains_tomorrows_workout(self, post, next_trainings):
        next_trainings.return_value = ([{
            "date": (date.today() + timedelta(days=1)).isoformat(),
            "type": "run",
            "name": "Fácil",
            "description": "Rodaje suave",
            "steps": [],
            "zones": {},
        }], True)
        post.return_value = Mock(raise_for_status=Mock(), json=Mock(return_value={"id": "test"}))

        with patch.dict(os.environ, {"RESEND_API_KEY": "test", "EMAIL_TO": "rafa@example.com"}):
            daily_job.send_email("training")

        message = post.call_args.kwargs["json"]
        self.assertIn("Entrenamiento de mañana", message["text"])
        self.assertNotIn("Cómo dormiste", message["text"])

    @patch.object(garmin_sync, "save_state")
    def test_removed_or_legacy_workout_is_deleted(self, save_state):
        client = Mock()
        state = {"2026-09-01 Legacy": {"date": "2026-09-01", "workout_id": 123}}

        garmin_sync.delete_tracked(client, state, valid_ids={"new-session"})

        client.delete_workout.assert_called_once_with(123)
        self.assertEqual({}, state)
        save_state.assert_called_once_with(state)

    def test_unified_plan_supports_all_sports_and_double_sessions(self):
        workouts = garmin_sync.load_workouts()
        self.assertEqual({"run", "bike", "swim", "strength"}, {item["type"] for item in workouts})
        self.assertEqual(len(workouts), len({item["id"] for item in workouts}))
        self.assertEqual(2, len([item for item in workouts if item["date"] == "2026-09-01"]))
        for kind, sport_id in (("run", 1), ("bike", 2), ("swim", 4), ("strength", 5)):
            workout = next(item for item in workouts if item["type"] == kind)
            self.assertEqual(sport_id, garmin_sync.workout_payload(workout)["sportType"]["sportTypeId"])


if __name__ == "__main__":
    unittest.main()
