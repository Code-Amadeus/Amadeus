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
            import os
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

            os.environ["HF_HUB_OFFLINE"] = "0"
            os.environ["TRANSFORMERS_OFFLINE"] = "0"

            runpy.run_path(str(sidecar), run_name="amadeus_sidecar_import_probe")
            import asr.qwen_model

            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"

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


    def test_launch_argument_is_validated_before_optional_model_loading(self) -> None:
        sidecar = PROJECT_ROOT / "asr/qwen3_asr_sidecar.py"
        for arguments, expected_code in ((["--help"], 0), (["--engine", "unsupported"], 2)):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, str(sidecar), *arguments], cwd=PROJECT_ROOT,
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                self.assertIn("--engine", completed.stdout + completed.stderr)
                self.assertNotIn("LOAD_FAIL", completed.stdout)

    def test_parent_passes_its_engine_as_a_launch_argument(self) -> None:
        from io import BytesIO
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from asr.backends import qwen3_asr as backend_module

        process = SimpleNamespace(
            poll=lambda: None, stdout=BytesIO(b'{"type": "ready"}\n'),
            stderr=BytesIO(), stdin=Mock(),
        )
        backend = backend_module.Qwen3ASRBackend(inference_engine="cuda_graph")
        with patch.object(backend, "_can_use_inprocess", return_value=False), \
                patch.object(backend, "_select_python", return_value=sys.executable), \
                patch.object(backend_module.Qwen3ASRBackend, "_shared_proc", None), \
                patch.dict(backend_module.os.environ, {"QWEN3_ASR_MODE": "sidecar",
                                                      "QWEN3_ASR_INFERENCE_ENGINE": "official"}), \
                patch.object(backend_module.subprocess, "Popen", return_value=process) as launch:
            backend.load("cpu")
            self.assertEqual(launch.call_args.args[0][2:], ["--engine", "cuda_graph"])
            self.assertEqual(launch.call_args.kwargs["env"]["QWEN3_ASR_INFERENCE_ENGINE"], "official")
            self.assertIs(backend._proc, process)


if __name__ == "__main__":
    unittest.main()
