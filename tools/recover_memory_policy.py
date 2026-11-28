"""Stop the backend, preserve originals, then rebuild verified derived policy."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Exact configured AMADEUS_MEMORY_DB_PATH")
    parser.add_argument("--user-id", required=True, help="Exact configured AMADEUS_MEMORY_USER_ID")
    args = parser.parse_args()
    try:
        result = recover_source_policy(args.db, args.user_id)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, PolicyRecoveryError) else (
            "store_already_owned" if str(exc) == "store_already_owned" else "recovery_unavailable")
        print(json.dumps({"state":"refused", "reason":reason,
            "action":"Stop the backend; verify configured identity. Restore matching core/policy backups or use an explicitly separate database and Session namespace if authority cannot be verified."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
