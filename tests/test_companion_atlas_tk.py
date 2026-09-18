import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from render.companion_atlas_tk import AtlasPlayer
from render.companion_pack import BYTE_LIMIT


def pack(root, size=2, count=4, columns=2):
    emotions = {}
    specs = []
    for name, color in (("a", (200, 0, 0, 128)), ("b", (0, 200, 0, 128)), ("c", (0, 0, 200, 128))):
        dimensions = (columns * size, ((count + columns - 1) // columns) * size)
        file = root / f"{name}.webp"
        image = Image.new("RGBA", dimensions, color)
        image.save(file, lossless=True)
        image.close()
        specs.append({"url": file.name, "size": size, "columns": columns,
                      "sequence": list(range(count)), "durationMs": 1020,
                      "decodedBytes": dimensions[0] * dimensions[1] * 4,
                      "fileBytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    emotions["normal"] = {"idle": specs[0], "speaking": specs[1], "speakingAlternate": specs[2]}
    manifest = {"format": "amadeus.companion-atlas.v1", "emotions": emotions}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_exact_source_tile_no_extra_alpha_tint_crop_or_fast_forward(tmp_path):
    manifest = pack(tmp_path)
    now = [0.0]
    player = AtlasPlayer(tmp_path, clock=lambda: now[0])
    player.select("normal", True)
    try:
        for seconds, tile in ((0, 0), (.3, 1), (.8, 3), (1.03, 0)):
            now[0] = seconds
            frame, delay = player.frame()
            assert frame.getpixel((0, 0)) == (0, 200, 0, 128)
            assert frame.size == (2, 2)
            frame.close()
            assert player.last_tile == tile
            assert delay > 0
        player.select("normal", True)
        assert player.spec["url"] == "b.webp", "repeated speaking must not switch variants"
        player.select("normal", False)
        player.select("normal", True)
        assert player.spec["url"] == "c.webp"
    finally:
        player.close()


def test_lru_bounds_and_pause_resume(tmp_path):
    pack(tmp_path, size=256, count=32, columns=8)  # each atlas is 8 MiB decoded
    now = [0.0]
    player = AtlasPlayer(tmp_path, clock=lambda: now[0])
    player.select("normal", False)
    first = player.entries["a.webp"]
    player.select("normal", True)
    assert player.resident_bytes == BYTE_LIMIT
    player.select("normal", False)
    player.select("normal", True)  # alternate evicts b, then select idle still cached
    assert len(player.entries) == 2 and player.resident_bytes == BYTE_LIMIT
    player.select("normal", False)
    now[0] = .5
    player.set_paused(True)
    frame, delay = player.frame()
    frame.close()
    assert delay is None
    before = player.last_tile
    now[0] = 10
    player.set_paused(False)
    frame, delay = player.frame()
    assert frame is None and player.last_tile == before and delay > 0
    player.close()
    assert player.resident_bytes == 0 and not player.entries
    with pytest.raises(ValueError):
        first.getpixel((0, 0))


def test_tk_timing_matches_actual_canvas_player(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node required for cross-renderer contract test")
    pack(tmp_path)
    renderer = Path(__file__).resolve().parents[1] / "render/web/companion_atlas.js"
    # Run the real JS player with synthetic decoded bitmap/canvas; no media/UI dependencies.
    script = r'''
const fs=require('node:fs'), vm=require('node:vm'); let now=0;
const manifest=JSON.parse(fs.readFileSync(process.argv[1]));
const scope={performance:{now:()=>now},URL,setTimeout:()=>0,clearTimeout(){},
 fetch:async()=>({ok:true,blob:async()=>({size:1})}),
 createImageBitmap:async()=>({width:4,height:4,close(){}})};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),scope);
const canvas={getContext:()=>({clearRect(){},drawImage(){}})};
(async()=>{const p=new scope.CompanionAtlas.Player(canvas,manifest,'http://localhost/');await p.select('normal',true);
 const out=[];for(now of [0,254,255,510,1019,1020,2300]){p.render();out.push(p.lastTile)};
 p.dispose();process.stdout.write(JSON.stringify(out));})();
'''
    expected = json.loads(subprocess.check_output(["node", "-e", script, str(tmp_path / "manifest.json"), str(renderer)], text=True))
    now = [0.0]
    player = AtlasPlayer(tmp_path, clock=lambda: now[0])
    player.select("normal", True)
    actual = []
    for ms in (0, 254, 255, 510, 1019, 1020, 2300):
        now[0] = ms / 1000
        frame, _ = player.frame()
        if frame is not None:
            frame.close()
        actual.append(player.last_tile)
    player.close()
    assert actual == expected


def test_static_tile_has_no_timer(tmp_path):
    pack(tmp_path, size=2, count=1, columns=1)
    player = AtlasPlayer(tmp_path)
    player.select("unknown", False, True)
    frame, delay = player.frame()
    frame.close()
    assert delay is None
    assert player.frame() == (None, None)
    player.close()
