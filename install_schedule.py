import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).resolve()
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"


def install(label: str, hour: int, night: bool) -> None:
    path = LAUNCH_AGENTS / f"{label}.plist"
    arguments = [str(PYTHON), str(ROOT / "daily_job.py")]
    if night:
        arguments.append("--night")
    payload = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "StandardOutPath": str(ROOT / "data" / f"{label}.log"),
        "StandardErrorPath": str(ROOT / "data" / f"{label}.error.log"),
    }
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootout", f"gui/{Path.home().stat().st_uid}", str(path)], check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{Path.home().stat().st_uid}", str(path)], check=True)
    print(f"Instalado {label} a las {hour:02d}:00")


if __name__ == "__main__":
    (ROOT / "data").mkdir(exist_ok=True)
    install("com.aitrainer.morning", 6, False)
    install("com.aitrainer.night", 21, True)
