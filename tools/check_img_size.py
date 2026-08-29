from PIL import Image
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "assets" / "images" / "normal" / "loop" / "kurisu_normal_loop0001.png"
img = Image.open(path)
print("size:", img.size)
print("width:", img.size[0], "height:", img.size[1])
