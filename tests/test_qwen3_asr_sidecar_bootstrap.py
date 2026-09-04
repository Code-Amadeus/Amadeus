from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Qwen3ASRSidecarBootstrapTest(unittest.TestCase):
    def test_direct_script_bootstraps_project_package_path(self) -> None:
        probe = textwrap.dedent(
            """
            import runpy
            import sys
            from pathlib import Path

            project_root = Path(sys.argv[1]).resolve()
            sidecar = project_root / "asr" / "qwen3_asr_sidecar.py"
            sys.path = [
                entry
                for entry in sys.path
                if Path(entry or ".").resolve() != project_root
            ]
            sys.path.insert(0, str(sidecar.parent))

            runpy.run_path(str(sidecar), run_name="amadeus_sidecar_import_probe")
            import asr.qwen_model

            print("sidecar-project-import-ok")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", probe, str(PROJECT_ROOT)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertIn("sidecar-project-import-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
