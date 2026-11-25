"""図解カード（SVG）を PNG にする。

Teams のカードの画像は SVG に対応しない（公式の文書）。PNG への変換には、PC に入っているブラウザー（Edge、Chrome、Chromium）の
ヘッドレスの機能を使う。新しいライブラリは足さない。ブラウザーは通信しない（開くのは、手元の 1 ファイルだけ）。

文字の形は、その PC に入っているフォントで決まる。日本語のフォントがない環境では、文字が出ない。だから、変換のあとで
  * 大きさが SVG と同じで、Teams の上限（1024×1024、1 MB 未満）に収まること
  * 白紙でないこと（文字や図が描かれていること）
を確かめ、通らなければ失敗にする。**文字が正しく読めるかは、人が画像を見て確かめる**（機械では確かめていない）。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

TEAMS_IMAGE_MAX_PX = 1024
TEAMS_IMAGE_MAX_BYTES = 1024 * 1024          # 「1 MB 未満」（公式の文書。2026-11-25 に確認）
MIN_INK_RATIO = 0.02                         # 背景と違う色の画素が、これより少なければ「描かれていない」とみなす


class PngError(RuntimeError):
    pass


def find_browser(override: str = "") -> str:
    """ヘッドレスで使えるブラウザーの実行ファイル。設定（AAR_BROWSER_BIN）があれば、それだけを使う。"""
    override = override or os.environ.get("AAR_BROWSER_BIN", "")
    if override:
        found = shutil.which(override) or (override if Path(override).is_file() else "")
        if not found:
            raise PngError(f"指定したブラウザーが見つからない: {override}")
        return found
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"), str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            candidates.append(w)
    for c in candidates:
        if Path(c).is_file():
            return c
    raise PngError("ヘッドレスで使えるブラウザー（Edge、Chrome、Chromium）が見つからない。場所を AAR_BROWSER_BIN で指定する")


def svg_size(svg: str) -> tuple[int, int]:
    m = re.search(r'<svg[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"', svg)
    if not m:
        raise PngError("SVG の大きさを読めない")
    return int(m.group(1)), int(m.group(2))


def read_png(data: bytes) -> dict[str, Any]:
    """PNG を読み、大きさと、背景（左上の画素）と違う色の画素の割合を返す。8 ビットの RGB / RGBA（インターレースなし）だけを扱う。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("PNG ではない")
    pos, idat, w = 8, b"", 0
    h = depth = ctype = interlace = 0
    while pos < len(data):
        (length,), kind = struct.unpack(">I", data[pos:pos + 4]), data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise PngError(f"扱えない形式の PNG（深さ {depth}、色の種類 {ctype}、インターレース {interlace}）")
    bpp = 3 if ctype == 2 else 4
    raw, stride = zlib.decompress(idat), w * bpp
    prev, different, background = bytearray(stride), 0, b""
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):                                   # 行ごとのフィルターを戻す
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        if y == 0:
            background = bytes(line[:3])
        if y % 4 == 0:                                            # 4 行に 1 行、4 画素に 1 画素を数える
            different += sum(1 for x in range(0, w, 4) if bytes(line[x * bpp:x * bpp + 3]) != background)
        prev = line
    sampled = ((h + 3) // 4) * ((w + 3) // 4)
    return {"width": w, "height": h, "ink_ratio": different / sampled if sampled else 0.0}


def svg_to_png(svg: str, out: Path, *, browser: str = "", timeout_s: int = 90) -> dict[str, Any]:
    """SVG を PNG に変換し、確かめてから out に書く。確かめに通らなければ、out は作らない（古いファイルも残さない）。"""
    exe = find_browser(browser)
    w, h = svg_size(svg)
    if w > TEAMS_IMAGE_MAX_PX or h > TEAMS_IMAGE_MAX_PX:
        raise PngError(f"カードが Teams の画像の上限（{TEAMS_IMAGE_MAX_PX}×{TEAMS_IMAGE_MAX_PX}）を超える: {w}×{h}")
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="aar-png-") as td:
        tmp = Path(td)
        page = tmp / "card.html"
        page.write_text('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;background:#fff;overflow:hidden}svg{display:block}</style>'
                        + svg, encoding="utf-8", newline="\n")
        shot = tmp / "card.png"
        # 追加の引数（AAR_BROWSER_ARGS）。既定では何も足さない。ブラウザー自身のサンドボックスを外す引数（--no-sandbox）は、
        # それが要る環境（一部の Linux の CI）で、人が明示したときだけ使う
        extra = shlex.split(os.environ.get("AAR_BROWSER_ARGS", ""))
        cmd = [exe, *extra, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={w},{h}", f"--user-data-dir={tmp / 'profile'}",
               f"--screenshot={shot}", page.as_uri()]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise PngError(f"ブラウザーが {timeout_s} 秒で終わらなかった（{exe}）") from None
        if not shot.exists():
            raise PngError(f"ブラウザーが画像を作らなかった（終了コード {r.returncode}。ヘッドレスの実行が、社内の設定で止められていないかを確かめる）: "
                           + " / ".join(r.stderr.decode("utf-8", "replace").strip().splitlines()[-6:])[-900:])
        data = shot.read_bytes()
    info = read_png(data)
    problems = []
    if (info["width"], info["height"]) != (w, h):
        problems.append(f"大きさが SVG と違う（{info['width']}×{info['height']}、期待は {w}×{h}）")
    if len(data) >= TEAMS_IMAGE_MAX_BYTES:
        problems.append(f"Teams の上限（1 MB 未満）を超える: {len(data)} バイト")
    if info["ink_ratio"] < MIN_INK_RATIO:
        problems.append(f"ほとんど何も描かれていない（背景と違う画素 {info['ink_ratio']:.1%}）。フォントがないか、描画に失敗している")
    if problems:
        raise PngError("変換した画像が確認に通らない: " + "; ".join(problems))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    v = subprocess.run([exe, "--version"], capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    return {"path": str(out), "width": w, "height": h, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "ink_ratio": round(info["ink_ratio"], 4), "browser": Path(exe).name, "extra_args": extra,
            "browser_version": v.stdout.decode("utf-8", "replace").strip()[:80],
            "note": "文字の形は、この PC のフォントによる。文字が正しく読めるかは、画像を見て確かめる"}
