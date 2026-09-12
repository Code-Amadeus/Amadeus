import sys
import os
from pathlib import Path
import torch

_cur_dir = Path(__file__).resolve().parent
_eres2net_dir = _cur_dir / "eres2net"
if str(_eres2net_dir) not in sys.path:
    sys.path.append(str(_eres2net_dir))

from ERes2NetV2 import ERes2NetV2
import kaldi as Kaldi


class SV:
    def __init__(self, device, is_half, sv_path=None):
        if sv_path is None:
            candidates = [
                _cur_dir.parent / "assets" / "models" / "gpt-sovits" / "pretrained" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
                _cur_dir / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
                Path(os.getcwd()) / "assets" / "models" / "gpt-sovits" / "pretrained" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
                Path(os.getcwd()) / "GPT_SoVITS" / "pretrained_models" / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt",
            ]
            for cand in candidates:
                if cand.is_file():
                    sv_path = str(cand)
                    break
            if sv_path is None:
                sv_path = str(candidates[0])
        pretrained_state = torch.load(sv_path, map_location="cpu", weights_only=False)
        embedding_model = ERes2NetV2(baseWidth=24, scale=4, expansion=4)
        embedding_model.load_state_dict(pretrained_state)
        embedding_model.eval()
        self.embedding_model = embedding_model
        if is_half == False:
            self.embedding_model = self.embedding_model.to(device)
        else:
            self.embedding_model = self.embedding_model.half().to(device)
        self.is_half = is_half
        self.device = device

    def compute_embedding3(self, wav):
        with torch.no_grad():
            wav = wav.to(self.device)
            if self.is_half == True:
                wav = wav.half()
            feat = torch.stack(
                [Kaldi.fbank(wav0.unsqueeze(0), num_mel_bins=80, sample_frequency=16000, dither=0) for wav0 in wav]
            )
            sv_emb = self.embedding_model.forward3(feat)
        return sv_emb

