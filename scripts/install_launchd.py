"""Install this checkout as a new per-user macOS LaunchAgent."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "com.kenta.polyu-ev-bot"
root = Path(__file__).resolve().parent.parent
plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
domain = f"gui/{os.getuid()}"

if sys.platform != "darwin":
    sys.exit("This installer is for macOS. Use Python directly or Docker elsewhere.")
if not (root / ".env").exists():
    sys.exit("Create .env and set your bot token before installing.")
if plist_path.exists():
    sys.exit("The service configuration already exists. Use launchctl kickstart after code updates.")
if subprocess.run(["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
    sys.exit("No GUI login session is available. Log in on this Mac before installing a LaunchAgent.")
data = root / "data"
data.mkdir(mode=0o700, exist_ok=True)
plist_path.parent.mkdir(parents=True, exist_ok=True)
config = {
    "Label": LABEL,
    "ProgramArguments": [sys.executable, "-m", "polyu_ev_bot"],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "Umask": 0o077,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(data / "bot.out.log"),
    "StandardErrorPath": str(data / "bot.err.log"),
}
with plist_path.open("xb") as stream:
    plistlib.dump(config, stream)
plist_path.chmod(0o600)
subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
print(f"Started {LABEL}. Logs: {data}")
