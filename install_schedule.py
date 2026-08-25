import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UV = shutil.which("uv") or "uv"
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
LOG_DIR = Path.home() / "Library/Logs/AITrainer"


def install(label: str, hour: int, email_flag: str) -> None:
    path = LAUNCH_AGENTS / f"{label}.plist"
    arguments = [UV, "run", "python", str(ROOT / "daily_job.py"), email_flag]
    payload = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "StandardOutPath": str(LOG_DIR / f"{label}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{label}.error.log"),
    }
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "bootout", f"gui/{Path.home().stat().st_uid}", str(path)], check=False)
    subprocess.run(["launchctl", "bootstrap", f"gui/{Path.home().stat().st_uid}", str(path)], check=True)
    print(f"Instalado {label} a las {hour:02d}:00")


def uninstall(label: str) -> None:
    path = LAUNCH_AGENTS / f"{label}.plist"
    if path.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{Path.home().stat().st_uid}", str(path)], check=False)
        path.unlink()


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for old_label in ("com.aitrainer.morning", "com.aitrainer.night", "com.aitrainer.presync"):
        uninstall(old_label)
    install("com.aitrainer.summary", 12, "--summary-email")
    install("com.aitrainer.training", 21, "--training-email")
