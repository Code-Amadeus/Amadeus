"""Offline model boundary; stdlib runner also works in the L4 ladder without dev extras."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from config.local_model_loading import enforce_local_model_loading


ROOT = Path(__file__).resolve().parents[1]

HF_PROBE = r'''
import os
import sys
from pathlib import Path

root, model_path, preimport = sys.argv[1:]
sys.path.insert(0, root)
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"

if preimport == "1":
    import requests
    import huggingface_hub
    import huggingface_hub.constants as constants
    from transformers.utils import hub as transformers_hub
    assert not constants.HF_HUB_OFFLINE
    assert not transformers_hub.is_offline_mode()
    # Include a cached/custom online session, not just cached boolean flags.
    huggingface_hub.configure_http_backend(backend_factory=requests.Session)
    previous_session = huggingface_hub.get_session()

from config.local_model_loading import enforce_local_model_loading
enforce_local_model_loading()

import requests
import huggingface_hub
import huggingface_hub.constants as constants
from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled
from transformers import AutoConfig, AutoModelForMaskedLM, BertConfig, BertForMaskedLM
from transformers.utils import hub as transformers_hub

assert constants.HF_HUB_OFFLINE
assert transformers_hub.is_offline_mode()
# Remote provider clients still get requests' ordinary transport; only Hub's
# own session factory is reconfigured by the local-model boundary.
assert type(requests.Session().get_adapter("https://")) is requests.adapters.HTTPAdapter
if preimport == "1":
    assert huggingface_hub.get_session() is not previous_session

# Any use of the ordinary network adapter is a test failure. OfflineAdapter
# should reject the operation earlier, so this test never contacts the network.
network_attempts = []
def forbidden_send(self, request, **kwargs):
    network_attempts.append(request.url)
    raise AssertionError("Network transport reached: " + request.url)
requests.adapters.HTTPAdapter.send = forbidden_send

for scheme in ("http", "https"):
    try:
        huggingface_hub.get_session().get(scheme + "://example.invalid/model.json")
    except OfflineModeIsEnabled:
        pass
    else:
        raise AssertionError("Hub HTTP transport was not blocked")

try:
    huggingface_hub.hf_hub_download(
        "amadeus-offline-test/does-not-exist", "config.json", local_files_only=False
    )
except LocalEntryNotFoundError:
    pass
else:
    raise AssertionError("Unexpected remote model artifact")

try:
    AutoConfig.from_pretrained("amadeus-offline-test/does-not-exist", local_files_only=False)
except OSError:
    pass
else:
    raise AssertionError("Unexpected remote Transformers config")

# A locally generated tiny model remains usable; no external weights/downloads.
config = BertConfig(vocab_size=16, hidden_size=8, num_hidden_layers=1,
                    num_attention_heads=2, intermediate_size=16)
model = BertForMaskedLM(config)
model.save_pretrained(model_path, safe_serialization=True)
loaded = AutoModelForMaskedLM.from_pretrained(model_path, local_files_only=True)
assert loaded.config.hidden_size == 8
assert not network_attempts, network_attempts
print("offline-network-blocked-and-local-model-loaded")
'''


class LocalModelLoadingTest(unittest.TestCase):
    def test_cold_policy_does_not_import_optional_dependencies(self):
        probe = (
            "import os,sys; sys.path.insert(0,sys.argv[1]); "
            "os.environ['HF_HUB_OFFLINE']='0'; os.environ['TRANSFORMERS_OFFLINE']='0'; "
            "from config.local_model_loading import enforce_local_model_loading; "
            "enforce_local_model_loading(); "
            "assert os.environ['HF_HUB_OFFLINE']=='1'; "
            "assert os.environ['TRANSFORMERS_OFFLINE']=='1'; "
            "assert not {'torch','transformers','huggingface_hub'} & sys.modules.keys()"
        )
        subprocess.run([sys.executable, "-S", "-c", probe, str(ROOT)], check=True, timeout=20)

    def test_preimported_flags_and_session_factory_are_reset(self):
        constants = SimpleNamespace(HF_HUB_OFFLINE=False)
        http = SimpleNamespace(configure_http_backend=Mock())
        transformers_hub = SimpleNamespace(_is_offline_mode=False)
        with (
            patch.dict(os.environ, HF_HUB_OFFLINE="0", TRANSFORMERS_OFFLINE="0"),
            patch.dict(sys.modules, {
                "huggingface_hub.constants": constants,
                "huggingface_hub.utils._http": http,
                "transformers.utils.hub": transformers_hub,
            }),
        ):
            enforce_local_model_loading()
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
            self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
            self.assertTrue(constants.HF_HUB_OFFLINE)
            self.assertTrue(transformers_hub._is_offline_mode)
            http.configure_http_backend.assert_called_once_with()

    def test_real_libraries_with_inherited_online_environment(self):
        self._run_hf_probe(preimport=False)

    def test_real_libraries_with_cached_online_state_and_session(self):
        self._run_hf_probe(preimport=True)

    def _run_hf_probe(self, *, preimport):
        if any(importlib.util.find_spec(name) is None for name in (
            "torch", "transformers", "huggingface_hub", "safetensors"
        )):
            self.skipTest("real HF model loading requires the local-model tier")
        with tempfile.TemporaryDirectory(prefix="amadeus-offline-model-") as directory:
            env = dict(os.environ)
            env["HF_HOME"] = str(Path(directory) / "empty-hf-cache")
            env["HF_HUB_CACHE"] = str(Path(directory) / "empty-hf-cache" / "hub")
            env["PYTHONUTF8"] = "1"
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", HF_PROBE, str(ROOT),
                 str(Path(directory) / "tiny-model"), "1" if preimport else "0"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("offline-network-blocked-and-local-model-loaded", result.stdout)


if __name__ == "__main__":
    unittest.main()
