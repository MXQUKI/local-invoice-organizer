#!/usr/bin/env python3
"""Convert the supplied icon.png to a Windows .ico build asset."""
from pathlib import Path

from PIL import Image

root = Path(__file__).resolve().parents[1]
source = root / "icon.png"
target = root / "build" / "本地发票整理.ico"
target.parent.mkdir(parents=True, exist_ok=True)
image = Image.open(source).convert("RGBA")
image.save(target, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(target)
