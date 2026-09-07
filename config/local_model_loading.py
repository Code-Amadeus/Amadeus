"""Offline Hugging Face boundary for the optional ASR/TTS/RAG model runtimes.

The qualified model profile pins huggingface-hub 0.36.2 / Transformers 4.57.6.
Both cache offline state on import; Hub also caches HTTP sessions. Set the
environment for future imports and update already-loaded state before importing
or loading voice models. This does not change the HTTP clients of remote Chat,
ASR or TTS providers. Explicit asset-download tools run in a separate process.
"""

from __future__ import annotations

import os
import sys


def enforce_local_model_loading() -> None:
    """Require offline model loading, including after a prior online Hub import.

    Do not import optional libraries here: the model-less baseline must remain
    importable. The cached-state assignments below target the pinned versions;
    their behavior is covered by the local-model integration test.
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    hub_constants = sys.modules.get("huggingface_hub.constants")
    if hub_constants is not None:
        hub_constants.HF_HUB_OFFLINE = True
    hub_http = sys.modules.get("huggingface_hub.utils._http")
    if hub_http is not None:
        # Restore Hub's standard factory, which now installs OfflineAdapter,
        # and invalidate any sessions created while Hub was still online.
        hub_http.configure_http_backend()
    transformers_hub = sys.modules.get("transformers.utils.hub")
    if transformers_hub is not None:
        transformers_hub._is_offline_mode = True
