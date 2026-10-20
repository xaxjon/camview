#!/usr/bin/env python3
"""Focused still-image dewarp test using a synthetic square fisheye."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix="camview-dewarp-") as tmp:
    work = Path(tmp)
    source = work / "raw.jpg"
    destination = work / "pano.jpg"
    rotated = work / "pano180.jpg"
    pixels = np.zeros((1280, 1280, 3), dtype=np.uint8)
    pixels[:640] = [255, 0, 0]
    pixels[640:] = [0, 0, 255]
    pixels[:, :100] = [0, 255, 0]
    pixels[:, 1180:] = [255, 255, 0]
    Image.fromarray(pixels).save(source, quality=95)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    subprocess.run([
        sys.executable, str(ROOT / "dewarp.py"), str(source), str(destination),
        "512", "0.519201", "0.499414", "0.494428",
    ], check=True)
    subprocess.run([
        sys.executable, str(ROOT / "dewarp.py"), str(source), str(rotated),
        "512", "0.519201", "0.499414", "0.494428", "180",
    ], check=True)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before, "raw JPEG changed"
    with Image.open(destination) as out:
        assert out.size == (512, 513), out.size
        top = out.getpixel((256, 128))
        bottom = out.getpixel((256, 385))
        separator = out.getpixel((256, 256))
        outer = out.getpixel((2, 2))
        assert top[2] > top[0], f"top strip is not the source bottom: {top}"
        assert bottom[0] > bottom[2], f"bottom strip is not the source top: {bottom}"
        assert max(separator) < 20, f"separator is not black: {separator}"
        assert outer[0] > 180 and outer[1] > 180 and outer[2] < 100, outer
    with Image.open(rotated) as out:
        rotated_outer = out.getpixel((2, 2))
        assert rotated_outer[1] > 180 and rotated_outer[0] < 100, rotated_outer
    assert not list(work.glob(".*.tmp")), "temporary output was left behind"

print("still dewarp mapping: ok (512x513, orientation, separator, offset, raw preserved)")
