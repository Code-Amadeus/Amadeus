"""Known #233 false-empty/no-op-close diagnostic; NEVER an acceptance pass."""
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
        print(json.dumps(dict(diagnostic="known false-empty protocol counterexample; not a recovery acceptance pass",
                              error=str(error), actual=[asdict(x) for x in fake.items], reported=fake.get_wallpapers(),
                              closeReturnedWithoutRemoval=bool(fake.closed and fake.items),
                              journalRetained=session.journal.exists(), stopRequested=fake.stopped is not None,
                              strongRecoveryProven=False), indent=2))


if __name__ == "__main__":
    main()
