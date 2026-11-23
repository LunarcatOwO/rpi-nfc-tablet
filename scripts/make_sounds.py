# rpi-nfc-tablet: touch NFC sign-in kiosk for a Raspberry Pi 4 B
# Created by LunarcatOwO (https://github.com/LunarcatOwO)
# Copyright (C) 2026 LunarcatOwO
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It comes WITHOUT ANY WARRANTY; see the LICENSE file.

"""Make the kiosk's sound files (nfc_login/ui/sounds/*.wav): short wooden knocks.

The screen's speakers are tiny, so the knock sits around 0.7-3 kHz (where they're
loud), has extra overtones and is saturated to be as loud as it can be. Events
differ by how many knocks there are and how high they sit.
Needs numpy: pip install numpy && python3 scripts/make_sounds.py
"""

import wave
from pathlib import Path

import numpy as np

SR = 44100
BASE = 700          # Hz; lower sounds deeper but quieter on the screen's speakers
OUT = Path(__file__).resolve().parent.parent / "nfc_login" / "ui" / "sounds"
rng = np.random.default_rng(7)

# name: [(pitch x BASE, start s, loudness)]
EVENTS = {
    "sign_in": [(1, 0, 1)],
    "sign_out": [(1, 0, .75), (.75, .09, 1)],
    "success": [(1.5, 0, .85)],
    "admin": [(1, 0, .8), (1.5, .08, .75)],
    "attention": [(1, 0, .9), (1, .13, .9), (1, .26, .9)],
    "warning": [(.75, 0, 1), (.75, .15, 1)],
    "error": [(.6, 0, 1), (.6 * .94, .13, 1)],
    "ignored": [(1, 0, .35)],
    "tap": [(2, 0, .3)],
}


def bandpass(x, f, q):
    w = 2 * np.pi * f / SR
    a, c = np.sin(w) / (2 * q), np.cos(w)
    b0, b2, a1, a2 = a / (1 + a), -a / (1 + a), -2 * c / (1 + a), (1 - a) / (1 + a)
    y, x1, x2, y1, y2 = np.zeros_like(x), 0.0, 0.0, 0.0, 0.0
    for i, v in enumerate(x):
        o = b0 * v + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, o
        y[i] = o
    return y


def knock(f, short=False):
    t = np.arange(int(SR * (.06 if short else .26))) / SR
    k = 55 if short else 16
    tone = (np.sin(2 * np.pi * f * t)
            + .6 * np.sin(4 * np.pi * f * t) * np.exp(-t * 10)
            + .35 * np.sin(6 * np.pi * f * t) * np.exp(-t * 20)) * np.exp(-t * k)
    body = bandpass(rng.standard_normal(len(t)), f * 2.3, 5) * np.exp(-t * 40) * .9
    click = np.zeros(len(t))
    click[:int(SR * .003)] = rng.standard_normal(int(SR * .003))
    x = tone + body + bandpass(click, 2500, 1.2) * 1.5
    return np.tanh(2.5 * x / np.max(np.abs(x)))


def save(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


for name, hits in EVENTS.items():
    parts = [(knock(BASE * m, name == "tap"), start, gain) for m, start, gain in hits]
    out = np.zeros(int(SR * (max(s + len(p) / SR for p, s, _ in parts) + .02)))
    for p, start, gain in parts:
        out[int(SR * start):int(SR * start) + len(p)] += p * gain
    out = np.tanh(1.5 * out / np.max(np.abs(out)))
    save(OUT / f"{name}.wav", out / np.max(np.abs(out)) * (.6 if name == "tap" else .97))
    print("made", name)
