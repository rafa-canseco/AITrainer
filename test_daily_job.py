import os
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import daily_job


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

    @patch.object(daily_job, "next_training")
    @patch.object(daily_job.httpx, "post")
    def test_training_email_only_contains_tomorrows_workout(self, post, next_training):
        next_training.return_value = ({
            "date": (date.today() + timedelta(days=1)).isoformat(),
            "type": "run",
            "name": "Fácil",
            "description": "Rodaje suave",
            "steps": [],
            "zones": {},
        }, True)
        post.return_value = Mock(raise_for_status=Mock(), json=Mock(return_value={"id": "test"}))

        with patch.dict(os.environ, {"RESEND_API_KEY": "test", "EMAIL_TO": "rafa@example.com"}):
            daily_job.send_email("training")

        message = post.call_args.kwargs["json"]
        self.assertIn("Entrenamiento de mañana", message["text"])
        self.assertNotIn("Cómo dormiste", message["text"])


if __name__ == "__main__":
    unittest.main()
