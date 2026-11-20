"""Speaker encoder used by GPT-SoVITS v2Pro and v2ProPlus."""

from pathlib import Path
import sys

import torch


_ERES2NET_DIR = Path(__file__).resolve().parent / "eres2net"
if str(_ERES2NET_DIR) not in sys.path:
    sys.path.insert(0, str(_ERES2NET_DIR))

from ERes2NetV2 import ERes2NetV2  # noqa: E402
import kaldi as Kaldi  # noqa: E402


class SV:
    def __init__(self, device, is_half, model_path):
        pretrained_state = torch.load(model_path, map_location="cpu", weights_only=True)
        embedding_model = ERes2NetV2(baseWidth=24, scale=4, expansion=4)
        embedding_model.load_state_dict(pretrained_state)
        embedding_model.eval()
        self.embedding_model = embedding_model.half().to(device) if is_half else embedding_model.to(device)
        self.is_half = is_half

    def compute_embedding3(self, wav):
        with torch.no_grad():
            if self.is_half:
                wav = wav.half()
            feat = torch.stack(
                [
                    Kaldi.fbank(
                        wav0.unsqueeze(0),
                        num_mel_bins=80,
                        sample_frequency=16000,
                        dither=0,
                    )
                    for wav0 in wav
                ]
            )
            return self.embedding_model.forward3(feat)
