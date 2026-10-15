"""Extract profile bridge and project execution status for hermes_bots.ps1."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import yaml


def main() -> int:
    if len(sys.argv) < 2:
        return 1
    config_path = Path(sys.argv[1])
    if not config_path.is_file():
        return 1
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    anti = data.get("antigravity_cli", {})
    proj = anti.get("project_execution", {})
    result = {
        "bridge_enabled": anti.get("experimental_subscription_bridge") is True,
        "project_enabled": proj.get("enabled") is True,
        "workspace": proj.get("workspace", ""),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
