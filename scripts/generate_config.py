import os
import sys
from pathlib import Path

FIELDS = [
    ("TENANT_ID", "NBS_TENANT_ID"),
    ("CLIENT_ID", "NBS_CLIENT_ID"),
    ("WORKSPACE_ID", "NBS_WORKSPACE_ID"),
    ("DATAFLOW_ID", "NBS_DATAFLOW_ID"),
    ("DATASET_ID", "NBS_DATASET_ID"),
    ("BATCH_REPORT_ID", "NBS_BATCH_REPORT_ID"),
]


def main() -> int:
    values = {}
    missing = []
    for const, env in FIELDS:
        value = os.environ.get(env, "").strip()
        if value:
            values[const] = value
        else:
            missing.append(env)
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        return 1
    target = Path(__file__).resolve().parent.parent / "config_generated.py"
    lines = [f"{const} = {values[const]!r}\n" for const, _ in FIELDS]
    target.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
