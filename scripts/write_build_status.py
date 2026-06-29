"""Update docs/data/build_status.json with CI data-source step status.

Usage:
  python scripts/write_build_status.py STEP ok|fail [message]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("docs/data/build_status.json")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: write_build_status.py STEP ok|fail [message]", file=sys.stderr)
        return 2
    step, state = sys.argv[1], sys.argv[2].lower()
    message = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    except json.JSONDecodeError:
        data = {}
    data.setdefault("steps", {})[step] = {
        "ok": state in {"ok", "success", "true", "1"},
        "message": message[:500],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {OUT}: {step}={data['steps'][step]['ok']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
