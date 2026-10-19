#!/usr/bin/env python3
"""Dewarp one circular-fisheye JPEG into a dual-panorama JPEG.

Usage: dewarp.py <source> <destination> <max-width> <cx> <cy> <radius>

Circle values are normalized: cx by source width, cy and radius by source
height. The destination is capped at the source width and written atomically.
"""
import argparse
import os
from pathlib import Path

import numpy as np
from PIL import Image


def output_width(value):
    width = int(value)
    if not 2 <= width <= 4096:
        raise argparse.ArgumentTypeError("width must be between 2 and 4096")
    return width


def dewarp(source, destination, requested_width, circle):
    with Image.open(source) as image:
        src = np.array(image.convert("RGB"), dtype=np.uint8, copy=True)

    source_height, source_width = src.shape[:2]
    width = max(2, min(requested_width, source_width))
    separator = max(1, (width + 256) // 512)
    height = width + separator
    strip_height = (height - separator) * 0.5

    cx = np.float32(circle[0] * source_width)
    cy = np.float32(circle[1] * source_height)
    radius = np.float32(circle[2] * source_height)
    u = (np.arange(width, dtype=np.float32) + np.float32(0.5)) / np.float32(width)
    cos_phi = []
    sin_phi = []
    for strip in (0, 1):
        phi = (np.float32(strip) + u) * np.float32(np.pi)
        cos_phi.append(np.cos(phi))
        sin_phi.append(np.sin(phi))

    out = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        py = np.float32(y + 0.5)
        if strip_height <= py < strip_height + separator:
            continue
        strip = 0 if py < strip_height else 1
        y_local = py - np.float32(strip * (strip_height + separator))
        rho = (np.float32(1.0) - y_local / np.float32(strip_height)) * radius
        sx = np.clip(cx + rho * cos_phi[strip], 0, source_width - 1)
        sy = np.clip(cy + rho * sin_phi[strip], 0, source_height - 1)

        x0 = sx.astype(np.int32)
        y0 = sy.astype(np.int32)
        x1 = np.minimum(x0 + 1, source_width - 1)
        y1 = np.minimum(y0 + 1, source_height - 1)
        dx = (sx - x0).astype(np.float32)[:, None]
        dy = (sy - y0).astype(np.float32)[:, None]

        a = src[y0, x0].astype(np.float32)
        b = src[y0, x1].astype(np.float32)
        c = src[y1, x0].astype(np.float32)
        d = src[y1, x1].astype(np.float32)
        top = a + (b - a) * dx
        bottom = c + (d - c) * dx
        row = top + (bottom - top) * dy
        out[y] = np.clip(row + np.float32(0.5), 0, 255).astype(np.uint8)

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        Image.fromarray(out).save(
            tmp, format="JPEG", quality=90, optimize=True, subsampling=0)
        os.replace(tmp, destination)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("width", type=output_width)
    parser.add_argument("cx", type=float)
    parser.add_argument("cy", type=float)
    parser.add_argument("radius", type=float)
    args = parser.parse_args()
    try:
        dewarp(args.source, args.destination, args.width,
               (args.cx, args.cy, args.radius))
    except Exception as e:
        parser.exit(1, f"dewarp: {e}\n")


if __name__ == "__main__":
    main()
