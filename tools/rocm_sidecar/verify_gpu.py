"""Check the selected ROCm environment; optionally run a tiny compute smoke test."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compute", action="store_true", help="Run a small FP32 matrix multiplication.")
    args = parser.parse_args()
    import torch

    module_path = Path(torch.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    isolated = module_path.is_relative_to(prefix)
    available = bool(torch.cuda.is_available())
    info = {
        "python": sys.executable,
        "prefix": str(prefix),
        "torch": torch.__version__,
        "torch_path": str(module_path),
        "torch_is_in_this_venv": isolated,
        "hip": torch.version.hip,
        "cuda_build": torch.version.cuda,
        "gpu_available": available,
        "gpu_names": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if available else [],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if not isolated:
        raise SystemExit("FAIL: torch was imported from outside this venv; inspect .pth / PYTHONPATH.")
    if not torch.version.hip or not available:
        raise SystemExit("FAIL: this interpreter does not have a usable ROCm PyTorch GPU.")
    if args.compute:
        # Unsupported Windows GPUs can make amdhip64 terminate the process with
        # an access violation. Isolate the kernel probe so this verifier can
        # report that failure instead of disappearing with the driver process.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import torch; "
                    "x=torch.ones((256,256),device='cuda:0',dtype=torch.float32); "
                    "y=x@x; torch.cuda.synchronize(); "
                    "assert bool(torch.isfinite(y).all()); "
                    "assert torch.allclose(y,torch.full_like(y,256.0))"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe.returncode:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            tail = detail[-1] if detail else "no diagnostic output"
            raise SystemExit(
                "FAIL: ROCm GPU compute subprocess failed "
                f"(exit={probe.returncode}): {tail}"
            )
        print("PASS: selected ROCm environment and FP32 GPU compute.")
    else:
        print("PASS: ROCm environment detected; compute/model correctness not yet tested.")


if __name__ == "__main__":
    main()
