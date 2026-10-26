"""FFmpeg editing engine: executes one planned output from the creator's original files.

Steps for an output: (silence-trimmed) segments → reframed, normalised clips → optional intro/outro cards →
concatenation → audio mix (original speech + optional original music) with loudness normalisation → optional
burned-in subtitles (drawn with Pillow, so Hindi renders correctly) → thumbnail. The original uploads are only
ever read. Every number in a filter graph is computed here; no model text reaches a command line.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from .. import config
from ..creative import music
from ..media import graphics
from ..uploads import probe

FPS = 30
SR = 48000
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "160k", "-ar", str(SR), "-ac", "2"]


class EditError(Exception):
    pass


def ff(args: list[str], timeout: int = 7200) -> str:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise EditError("FFmpeg failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1][:300])
    return r.stderr


def target_size(aspect: str, src_w: int | None, src_h: int | None) -> tuple[int, int]:
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "1:1":
        return 1080, 1080
    w, h = src_w or 1920, src_h or 1080
    scale = min(1.0, 1920 / max(w, h))
    return int(w * scale) // 2 * 2, int(h * scale) // 2 * 2


# ------------------------------------------------------------------ analysis

def scene_cuts(path: str, duration: float) -> list[float]:
    """Shot changes (seconds), detected on a small copy of the picture."""
    if not duration:
        return []
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-i", path, "-an", "-vf",
                        "scale=160:-2,select='gt(scene,0.32)',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=max(120, int(duration * 2)))
    cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    return [round(c, 2) for c in cuts if 0.5 < c < duration - 0.5][:400]


def silences(path: str, start: float, end: float, noise_db: int = -35, min_len: float = 0.6) -> list[tuple[float, float]]:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", path,
                        "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=3600)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", r.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else end - start
        out.append((start + max(0.0, s), start + e))
    return out


def keep_intervals(start: float, end: float, quiet: list[tuple[float, float]], pad: float = 0.15) -> list[tuple[float, float]]:
    """The segment minus long pauses (a little breathing room kept on both sides of each cut)."""
    keep, cur = [], start
    for s, e in quiet:
        s, e = max(start, s + pad), min(end, e - pad)
        if e - s < 0.3:
            continue
        if s - cur > 0.25:
            keep.append((cur, s))
        cur = e
    if end - cur > 0.25:
        keep.append((cur, end))
    return keep or [(start, end)]


# ------------------------------------------------------------------ clips

def cut_clip(src: dict[str, Any], start: float, end: float, w: int, h: int, reframe: str, speed: float,
             out: Path) -> None:
    dur = max(0.1, end - start)
    vf_speed = f",setpts=PTS/{speed:.4f}" if speed != 1 else ""
    af = f"aresample={SR},aformat=channel_layouts=stereo" + (f",atempo={speed:.4f}" if speed != 1 else "")
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src["path"]]
    if src["kind"] == "audio":                   # audio-only source: plain branded background
        args = ["-f", "lavfi", "-t", f"{dur / speed:.3f}", "-i", f"color=c=0x101828:s={w}x{h}:r={FPS}", *args]
        vmap, amap = "[0:v]", "[1:a]"
        graph = f"{vmap}format=yuv420p[v];{amap}{af}[a]"
    else:
        if reframe == "crop":
            v = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
        else:
            v = (f"[0:v]split[a][b];[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
                 f"crop={w // 4}:{h // 4},boxblur=8:2,scale={w}:{h}[bg];"
                 f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
        graph = f"{v},fps={FPS}{vf_speed},format=yuv420p[v]"
        if src.get("has_audio"):
            graph += f";[0:a]{af}[a]"
        else:
            args += ["-f", "lavfi", "-t", f"{dur / speed:.3f}", "-i", f"anullsrc=r={SR}:cl=stereo"]
            graph += f";[1:a]anull[a]"
    ff([*args, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *X264, *AAC, "-r", str(FPS),
        "-movflags", "+faststart", str(out)])


def card_clip(text: str, w: int, h: int, seconds: float, out: Path, seed: str) -> None:
    img = graphics.text_card(text, w, h, seed)
    png = out.with_suffix(".png")
    img.convert("RGB").save(png)
    ff(["-loop", "1", "-t", f"{seconds:.2f}", "-i", str(png), "-f", "lavfi", "-t", f"{seconds:.2f}", "-i",
        f"anullsrc=r={SR}:cl=stereo", "-vf", f"scale={w}:{h},fps={FPS},format=yuv420p,fade=t=in:st=0:d=0.4,"
        f"fade=t=out:st={max(0.1, seconds - 0.4):.2f}:d=0.4", *X264, *AAC, "-shortest", str(out)])


def concat(clips: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])


# ------------------------------------------------------------------ subtitles

def cues(transcript: list[dict[str, Any]], pieces: list[dict[str, Any]], max_words: int = 7) -> list[dict[str, Any]]:
    """Map source-time words onto the output timeline. pieces: [{src_start, src_end, out_start, speed}]."""
    out = []
    for pc in pieces:
        words = []
        for seg in transcript:
            if seg["end"] <= pc["src_start"] or seg["start"] >= pc["src_end"]:
                continue
            ws = seg.get("words") or [{"start": seg["start"], "end": seg["end"], "word": seg["text"]}]
            words += [w for w in ws if pc["src_start"] <= (w["start"] + w["end"]) / 2 < pc["src_end"]]
        for i in range(0, len(words), max_words):
            group = words[i:i + max_words]
            text = " ".join(w["word"].strip() for w in group).strip()
            if not text:
                continue
            s = pc["out_start"] + (max(group[0]["start"], pc["src_start"]) - pc["src_start"]) / pc["speed"]
            e = pc["out_start"] + (min(group[-1]["end"], pc["src_end"]) - pc["src_start"]) / pc["speed"]
            out.append({"start": round(s, 2), "end": round(max(e, s + 0.6), 2), "text": text})
    for a, b in zip(out, out[1:]):                 # no overlapping captions
        a["end"] = min(a["end"], b["start"])
    return [c for c in out if c["end"] - c["start"] >= 0.25]


def speech_lines(transcript: list[dict[str, Any]], pieces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whole spoken lines (transcript segments) on the output timeline — what a new voice re-speaks."""
    out = []
    for pc in pieces:
        for seg in transcript:
            a, b = max(seg["start"], pc["src_start"]), min(seg["end"], pc["src_end"])
            if b - a < 0.3 or not (seg.get("text") or "").strip():
                continue
            out.append({"start": pc["out_start"] + (a - pc["src_start"]) / pc["speed"],
                        "end": pc["out_start"] + (b - pc["src_start"]) / pc["speed"], "text": seg["text"].strip()})
    return sorted(out, key=lambda x: x["start"])


def revoice(joined: Path, lines: list[dict[str, Any]], voice_id: str, total: float, folder: Path) -> Path:
    """Replace the speech in `joined` with the same lines spoken in a cloned voice, each at its original time
    (sped up a little, at most 1.5x, when the new line is longer than its slot)."""
    import wave

    import numpy as np

    from ..media import voiceclone
    folder.mkdir(parents=True, exist_ok=True)
    rate = 24000
    track = np.zeros(int((total + 2) * rate), dtype=np.int32)
    for i, ln in enumerate(lines):
        audio, sr = voiceclone.say(ln["text"], voice_id, "")
        if sr != rate:
            audio = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * rate / sr)), np.arange(len(audio)),
                              audio.astype(np.float32)).astype(np.int16)
        slot = (lines[i + 1]["start"] if i + 1 < len(lines) else total) - ln["start"]
        if slot > 0.3 and len(audio) / rate > slot * 1.02:
            raw, fast = folder / f"l{i:04d}.wav", folder / f"l{i:04d}_fit.wav"
            with wave.open(str(raw), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(audio.tobytes())
            ff(["-i", str(raw), "-filter:a", f"atempo={min(len(audio) / rate / slot, 1.5):.3f}", "-ar", str(rate),
                "-ac", "1", str(fast)])
            with wave.open(str(fast), "rb") as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        at = int(ln["start"] * rate)
        end = min(len(track), at + len(audio))
        track[at:end] += audio[:end - at]
    voice = folder / "voice.wav"
    with wave.open(str(voice), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.clip(track, -32768, 32767).astype(np.int16).tobytes())
    out = folder / "revoiced.mp4"
    ff(["-i", str(joined), "-i", str(voice), "-map", "0:v", "-map", "1:a", "-c:v", "copy", *AAC,
        "-t", f"{total:.3f}", str(out)])
    return out


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cs: list[dict[str, Any]], out: Path) -> None:
    out.write_text("".join(f"{i}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n\n"
                           for i, c in enumerate(cs, 1)), encoding="utf-8")


def caption_layer(text: str, w: int, h: int, highlight: set[str]) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = int(min(w, h) * (0.062 if h > w else 0.05))
    fnt = graphics.font_for(text, "heavy", size)
    lines = graphics.wrap(text, fnt, int(w * 0.86), d)[:3]
    line_h = int(size * 1.35)
    y = int(h * (0.70 if h > w else 0.80)) - line_h * len(lines) // 2
    for ln in lines:
        words = ln.split()
        total = d.textlength(ln, font=fnt)
        x = (w - total) / 2
        for wd in words:
            key = re.sub(r"[^\w]", "", wd.lower(), flags=re.UNICODE)
            colour = (255, 214, 0) if key and key in highlight else (255, 255, 255)
            d.text((x, y), wd, font=fnt, fill=colour, stroke_width=max(3, size // 12), stroke_fill=(0, 0, 0))
            x += d.textlength(wd + " ", font=fnt)
        y += line_h
    return img


def subtitle_track(cs: list[dict[str, Any]], w: int, h: int, total: float, highlight: list[str], folder: Path) -> Path:
    """A transparent image sequence (concat list with durations) that FFmpeg overlays on the video."""
    folder.mkdir(parents=True, exist_ok=True)
    keys = {re.sub(r"[^\w]", "", k.lower(), flags=re.UNICODE) for k in highlight if k.strip()}
    blank = folder / "blank.png"
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(blank)
    entries, t = [], 0.0
    for i, c in enumerate(cs):
        if c["start"] > t:
            entries.append((blank, c["start"] - t))
        png = folder / f"c{i:05d}.png"
        caption_layer(c["text"], w, h, keys).save(png)
        entries.append((png, c["end"] - c["start"]))
        t = c["end"]
    entries.append((blank, max(0.1, total - t)))
    lst = folder / "track.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\nduration {d:.3f}\n" for p, d in entries) +
                   f"file '{entries[-1][0].resolve()}'\n")
    return lst


# ------------------------------------------------------------------ final pass

def finish(joined: Path, out: Path, *, music_mood: str, subs: Path | None, total: float, work: Path) -> None:
    inputs = ["-i", str(joined)]
    graph, vmap = [], "0:v"
    if subs is not None:
        inputs += ["-f", "concat", "-safe", "0", "-i", str(subs)]
        graph.append("[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]")
        vmap = "[v]"
    if music_mood and music_mood != "none":
        bed = music.bed(music_mood, total + 1, work / "music.wav")
        idx = len([x for x in inputs if x == "-i"])
        inputs += ["-i", str(bed)]
        graph.append(f"[{idx}:a]volume=0.13,aresample={SR},aformat=channel_layouts=stereo[m];"
                     f"[0:a][m]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    else:
        graph.append("[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    video = [*X264] if subs is not None else ["-c:v", "copy"]
    ff([*inputs, "-filter_complex", ";".join(graph), "-map", vmap, "-map", "[a]", *video, *AAC,
        "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)])


def thumbnail(video: Path, title: str, out: Path, at: float) -> Path:
    frame = out.with_suffix(".frame.png")
    ff(["-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1", str(frame)])
    img = graphics.cover(Image.open(frame).convert("RGB"), config.THUMB_W, config.THUMB_H)
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle([0, int(img.height * 0.58), img.width, img.height], fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img.convert("RGBA"), shade.filter(ImageFilter.GaussianBlur(18)))
    d = ImageDraw.Draw(img)
    fnt = graphics.font_for(title, "heavy", 78)
    lines = graphics.wrap(title, fnt, int(img.width * 0.9), d)[:2]
    y = img.height - 40 - 92 * len(lines)
    for ln in lines:
        d.text((60, y), ln, font=fnt, fill=(255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0))
        y += 92
    graphics.save_jpeg_under(img.convert("RGB"), out, 2_000_000)
    frame.unlink(missing_ok=True)
    return out


def check_output(path: Path, expect_w: int, expect_h: int, expect_s: float) -> list[dict[str, Any]]:
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": "fail", "detail": detail})
    add("file exists", path.exists() and path.stat().st_size > 10_000,
        f"{path.stat().st_size / 1e6:.1f} MB" if path.exists() else "missing")
    if not path.exists():
        return checks
    meta = probe(path)
    add("resolution", (meta["width"], meta["height"]) == (expect_w, expect_h),
        f"{meta['width']}x{meta['height']} (expected {expect_w}x{expect_h})")
    add("audio track", bool(meta["has_audio"]), meta.get("acodec") or "none")
    dur = meta.get("duration") or 0
    add("duration", abs(dur - expect_s) <= max(1.5, expect_s * 0.03), f"{dur:.1f}s (planned {expect_s:.1f}s)")
    return checks


def media_info(path: Path) -> dict[str, Any]:
    m = probe(path)
    m["size_bytes"] = path.stat().st_size
    m["aspect"] = f"{m['width']}:{m['height']}" if m.get("width") else None
    return json.loads(json.dumps(m))
