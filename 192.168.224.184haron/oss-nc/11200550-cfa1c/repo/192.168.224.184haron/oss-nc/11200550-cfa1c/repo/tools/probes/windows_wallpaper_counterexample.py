"""Replay #233 against the lifecycle helper, using a fake host (no desktop)."""
from dataclasses import asdict
import json
from pathlib import Path
import runpy
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    contract = runpy.run_path(str(ROOT / "tests/test_windows_wallpaper_helper.py"))
    helper, fake = contract["h"], contract["FakeHost"]()
    fake.reported, fake.close_noop, fake.fail_after = [], True, True
    with tempfile.TemporaryDirectory(prefix="amadeus-wallpaper-counterexample-") as temporary:
        session = helper.Session(fake, Path(temporary) / "session.json")
        error = contract["failure"](lambda: session.mount(contract["before"](fake, helper.ProcessIdentity(10, 20))))
        observation = dict(error=helper.error_detail(error), actual=[asdict(x) for x in fake.items],
                           reported=fake.get_wallpapers(), closeReturnedWithoutRemoval=bool(fake.closed and fake.items),
                           journalRetained=session.journal.exists(), stopRequested=fake.stopped is not None)
        fake.close_noop = False
        session.restore()
        retry = dict(actual=[asdict(x) for x in fake.items], journalRetained=session.journal.exists(),
                     stopRequested=fake.stopped is not None)
        print(json.dumps(dict(scope="offline #233 counterexample", failedClose=observation, retry=retry), indent=2))
        return 0 if (observation["closeReturnedWithoutRemoval"] and observation["journalRetained"]
                     and not observation["stopRequested"] and not retry["actual"]
                     and not retry["journalRetained"] and retry["stopRequested"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
