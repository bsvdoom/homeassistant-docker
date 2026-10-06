#!/usr/bin/env python3
"""Create a private recorder include from Compose's resolved DB settings."""
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import quote


def recorder_config(environment):
    user = quote(environment["MYSQL_USER"], safe="")
    password = quote(environment["MYSQL_PASSWORD"], safe="")
    database = quote(environment["MYSQL_DATABASE"], safe="")
    url = f"mysql://{user}:{password}@127.0.0.1/{database}?charset=utf8mb4"
    return f"db_url: {json.dumps(url)}\npurge_keep_days: 90\nauto_purge: true\n"


def main():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["docker", "compose", "config", "--environment"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode:
        raise SystemExit("Compose configuration failed. Check the required .env values.")
    # Canonical Compose JSON can escape dotenv dollar signs as $$ for replay.
    # Read interpolation inputs instead so URL passwords retain their exact bytes.
    environment = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    target = root / "homeassistant/config/recorder.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            output.write(recorder_config(environment))
    except FileExistsError:
        raise SystemExit("recorder.yaml already exists; update it manually if credentials changed.")
    print("Created private recorder.yaml. Include it with: recorder: !include recorder.yaml")


if __name__ == "__main__":
    main()
