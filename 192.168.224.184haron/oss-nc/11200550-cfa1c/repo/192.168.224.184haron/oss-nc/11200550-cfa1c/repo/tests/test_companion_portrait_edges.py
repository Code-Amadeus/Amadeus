"""Image-processing contracts: stable alpha edges without blurring facial animation."""
import importlib
from pathlib import Path

import numpy as np
from PIL import Image
import pytest


@pytest.fixture
def edges(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools/probes'))
    return importlib.import_module('refine_companion_edges')


def test_resize_does_not_brighten_constant_colour_on_transparent_boundary(edges):
    arr=np.zeros((400,400,4),dtype=np.uint8)
    arr[:,:,:3]=[120,80,40]
    arr[:,:201,3]=255
    result=np.asarray(edges.crop_alpha_correct(Image.fromarray(arr),132,.74,.035))
    band=(result[:,:,3]>70)&(result[:,:,3]<230)
    assert band.any()
    assert np.abs(result[:,:,:3][band].astype(int)-[120,80,40]).max()<=4


def test_temporal_filter_preserves_opaque_mouth_and_moving_silhouette(edges):
    frames=[]
    for alpha in [80,110,80]:
        arr=np.zeros((4,4,4),dtype=np.uint8)
        arr[:]=[100,70,40,alpha]
        arr[0,0]=[alpha,alpha,alpha,255]  # Changes here represent opaque facial motion.
        frames.append(Image.fromarray(arr))
    result,changed=edges.stabilize_edges(frames)
    assert changed>0
    for a,b in zip(frames,result):
        assert np.array_equal(np.asarray(a)[0,0],np.asarray(b)[0,0])
    assert 80<int(np.asarray(result[1])[1,1,3])<110
    # A large silhouette displacement must not acquire a temporal ghost trail.
    moving=[Image.new('RGBA',(4,4),(100,70,40,a)) for a in [0,180,255]]
    result,changed=edges.stabilize_edges(moving)
    assert changed==0
    assert all(a.tobytes()==b.tobytes() for a,b in zip(moving,result))
