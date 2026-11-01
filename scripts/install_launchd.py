"""Install this checkout's collector and Telegram bot as new LaunchAgents."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABELS = {"com.kenta.polyu-ev-collector": ["--collect"], "com.kenta.polyu-ev-bot": []}
root = Path(__file__).resolve().parent.parent
agents_dir = Path.home() / "Library" / "LaunchAgents"
domain = f"gui/{os.getuid()}"

if sys.platform != "darwin":
    sys.exit("This installer is for macOS. Use Python directly or Docker elsewhere.")
if not (root / ".env").exists():
    sys.exit("Create .env and set your bot token before installing.")
if any((agents_dir / f"{label}.plist").exists() for label in LABELS):
    sys.exit("The service configuration already exists. Use launchctl kickstart after code updates.")
if subprocess.run(["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
    sys.exit("No GUI login session is available. Log in on this Mac before installing a LaunchAgent.")
data = root / "data"
data.mkdir(mode=0o700, exist_ok=True)
agents_dir.mkdir(parents=True, exist_ok=True)
for label, args in LABELS.items():
    plist_path = agents_dir / f"{label}.plist"
    name = "collector" if args else "bot"
    config = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "polyu_ev_bot", *args],
        "WorkingDirectory": str(root),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(data / f"{name}.out.log"),
        "StandardErrorPath": str(data / f"{name}.err.log"),
    }
    with plist_path.open("xb") as stream:
        plistlib.dump(config, stream)
    plist_path.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    print(f"Started {label}. Logs: {data}")
