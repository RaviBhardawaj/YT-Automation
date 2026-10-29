"""What this computer is, what the studio needs on it, and one-click installs for the parts that can be added safely.

Detects the processor (Apple Silicon, Intel, AMD), the graphics (Apple GPU, NVIDIA, AMD) and memory/disk, then
lists each component with its status, why it matters, and how to get it: a background install task when the app
can do it itself (voice model, transcription model, voice cloning), or the exact command / link when it needs the
computer's owner (system packages, accounts and API keys). Free options are listed before paid ones.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config, db, logs, settings


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def machine() -> dict[str, Any]:
    os_name, arch = platform.system(), platform.machine().lower()
    cpu, gpu, ram_gb = platform.processor() or arch, "none detected", 0.0
    if os_name == "Darwin":
        cpu = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or cpu
        ram_gb = int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 1024 ** 3
        # an Intel Python on an Apple Silicon Mac still reports x86_64: ask the kernel
        apple = "Apple" in cpu or _run(["sysctl", "-n", "hw.optional.arm64"]) == "1"
        gpu = "Apple GPU (Metal)" if apple else (re.search(r"Chipset Model: (.+)", _run(
            ["system_profiler", "SPDisplaysDataType"])) or [None, "Intel/AMD graphics"])[1]
    elif os_name == "Linux":
        info = Path("/proc/cpuinfo").read_text(errors="ignore") if Path("/proc/cpuinfo").exists() else ""
        cpu = (re.search(r"model name\s*:\s*(.+)", info) or [None, cpu])[1]
        mem = Path("/proc/meminfo").read_text(errors="ignore") if Path("/proc/meminfo").exists() else ""
        ram_gb = int((re.search(r"MemTotal:\s+(\d+)", mem) or [None, 0])[1]) / 1024 ** 2
    elif os_name == "Windows":
        cpu = os.environ.get("PROCESSOR_IDENTIFIER", cpu)
        ram_gb = 0.0
    if shutil.which("nvidia-smi"):
        gpu = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]) or "NVIDIA GPU"
    elif shutil.which("rocm-smi"):
        gpu = "AMD GPU (ROCm)"
    kind = ("apple" if "Apple" in cpu or (os_name == "Darwin" and "Apple" in gpu) else
            "amd" if "AMD" in cpu.upper() else "intel" if "INTEL" in cpu.upper() else "other")
    disk = shutil.disk_usage(config.DATA_DIR)
    return {"os": {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(os_name, os_name),
            "os_version": platform.mac_ver()[0] if os_name == "Darwin" else platform.release(),
            "arch": arch, "cpu": cpu, "cpu_kind": kind, "gpu": gpu,
            "accelerated": kind == "apple" or "NVIDIA" in gpu.upper() or "ROCM" in gpu.upper(),
            "ram_gb": round(ram_gb, 1), "disk_free_gb": round(disk.free / 1024 ** 3, 1),
            "python": sys.version.split()[0], "brew": bool(shutil.which("brew")),
            "winget": bool(shutil.which("winget"))}


def _ollama_ready() -> bool:
    from .ai import providers
    try:
        return providers.get("ollama_local").available()[0]
    except Exception:
        return False


def _pkg_hint(m: dict[str, Any], mac: str, win: str, linux: str) -> str:
    return mac if m["os"] == "macOS" else win if m["os"] == "Windows" else linux


def components() -> list[dict[str, Any]]:
    from .media import tts, voiceclone
    m = machine()
    whisper_dir = config.DATA_DIR / "models" / "whisper"
    has_whisper = whisper_dir.exists() and any(whisper_dir.glob("models--*/snapshots/*/model.bin"))
    py312 = shutil.which("python3.12") or ("/opt/homebrew/bin/python3.12" if Path("/opt/homebrew/bin/python3.12").exists() else None)
    slow = "" if m["accelerated"] else " On this processor it runs on the CPU only — expect it to be very slow."
    out = [
        {"id": "ffmpeg", "group": "Required", "name": "FFmpeg (video engine)", "ok": bool(shutil.which(config.FFMPEG) or Path(config.FFMPEG).exists()),
         "why": "Cuts, renders and checks every video.", "free": True,
         "how": _pkg_hint(m, "brew install ffmpeg", "winget install Gyan.FFmpeg", "sudo apt install ffmpeg")},
        {"id": "kokoro", "group": "Voices", "name": "Kokoro voices (free, Hindi + English)", "ok": tts.kokoro_installed(),
         "why": "Free narration voice; also the automatic backup when a paid voice fails.", "free": True,
         "install": "kokoro_download", "size": "≈ 120 MB"},
        {"id": "whisper", "group": "Editing", "name": "Speech-to-text model (Whisper small)", "ok": has_whisper,
         "why": "Captions and smart cuts in Upload & edit; re-voicing with your cloned voice.", "free": True,
         "install": "whisper_download", "size": "≈ 460 MB"},
        {"id": "voiceclone", "group": "Voices", "name": "Voice cloning (Chatterbox, MIT)",
         "ok": voiceclone.installed() and voiceclone.model_ready(), "free": True,
         "why": "Lets each user narrate videos in their own voice." + slow,
         "install": "voiceclone_setup" if py312 else None, "size": "≈ 2 GB software + 6 GB model",
         "how": None if py312 else _pkg_hint(m, "brew install python@3.12", "winget install Python.Python.3.12",
                                             "sudo apt install python3.12 python3.12-venv"),
         "blocked": None if m["disk_free_gb"] > 10 else f"needs ~10 GB free disk (you have {m['disk_free_gb']} GB)"},
        {"id": "local_flux", "group": "Pictures", "name": "FLUX.2 klein on this computer (free, never runs out)",
         "ok": _local_flux_ok(), "free": True,
         "why": "Makes scene pictures on this Mac when the free online allowances are used up, so videos never "
                "wait. About a minute a picture." + ("" if m.get("os") == "macOS" and m["accelerated"] else
                                                     " Needs an Apple Silicon Mac."),
         "install": "local_flux_setup" if (py312 and m.get("os") == "macOS") else None, "size": "≈ 5 GB",
         "how": None if py312 else _pkg_hint(m, "brew install python@3.12", "-", "-"),
         "blocked": None if m["disk_free_gb"] > 8 else f"needs ~8 GB free disk (you have {m['disk_free_gb']} GB)"},
        {"id": "ollama_local", "group": "AI (optional)", "name": "Ollama on this computer (free, private AI)",
         "ok": bool(shutil.which("ollama")) and _ollama_ready(), "free": True,
         "why": "Runs writing models locally at no cost. Works best on Apple Silicon or an NVIDIA/AMD GPU with 16 GB+ memory." + slow,
         "how": (f"ollama pull {(settings.get('ai.models') or {}).get('ollama_local') or 'qwen3.5:9b'}"
                 if shutil.which("ollama") else
                 _pkg_hint(m, "brew install ollama", "winget install Ollama.Ollama", "curl -fsSL https://ollama.com/install.sh | sh"))},
    ]
    keys = [("ollama_api_key", "Ollama Cloud key", "Scripts, ideas and research (free plan included usage).", True),
            ("gemini_api_key", "Gemini key (free tier)", "Checks that every picture matches its scene; backup writer.", True),
            ("pexels_api_key", "Pexels key (free, optional)", "More HD stock footage. Without it the studio uses Pixabay plus Wikimedia Commons and Openverse (no key needed).", True),
            ("pixabay_api_key", "Pixabay key (free)", "Free stock videos and photos.", True),
            ("cloudflare_api_token", "Cloudflare Workers AI (free, ~150 AI images a day)",
             "Photo-real images for scenes with no matching stock, and kids' cartoon art. Add the token here and the "
             "account ID under Settings → Visuals.", True),
            ("youtube_api_key", "YouTube Data API key (free)", "Competitor and trend research.", True),
            ("sarvam_api_key", "Sarvam key (paid, best Hindi voices)", "Most natural Hindi voices; free Kokoro is used when absent.", False),
            ("anthropic_api_key", "Claude key (paid)", "Highest-quality writing; only used when free options fail.", False)]
    for name, label, why, free in keys:
        ok = settings.has_secret(name) and (name != "cloudflare_api_token"
                                            or bool((settings.get("visuals.cloudflare_account_id") or "").strip()))
        out.append({"id": name, "group": "Free API keys" if free else "Paid (optional)", "name": label,
                    "ok": ok, "why": why, "free": free,
                    "link": settings.SECRETS.get(name, {}).get("url"), "key": True})
    return out


def summary() -> dict[str, Any]:
    comps = components()
    need = [c for c in comps if not c["ok"] and c["group"] == "Required"]
    return {"machine": machine(), "components": comps, "missing_required": len(need),
            "missing_free": len([c for c in comps if not c["ok"] and c["free"] and c["group"] != "Paid (optional)"])}


# ------------------------------------------------------------------ installers (run as background tasks)

def _local_flux_ok() -> bool:
    from .media import visuals
    return visuals.local_flux_ready()


IPV4_ONLY = ("# Networks that advertise IPv6 they can't carry stall big downloads: resolve to IPv4 only.\n"
             "import socket\n_orig = socket.getaddrinfo\n\n\n"
             "def _ipv4_only(host, port, family=0, *args, **kwargs):\n"
             "    return _orig(host, port, socket.AF_INET if family in (0, socket.AF_UNSPEC) else family, *args, **kwargs)\n\n\n"
             "socket.getaddrinfo = _ipv4_only\n")


def _ipv4_only(venv: Path) -> None:
    """Loaded at start-up through a .pth file (a sitecustomize.py would lose to Homebrew Python's own)."""
    for sp in venv.glob("lib/python3*/site-packages"):
        (sp / "ipv4only.py").write_text(IPV4_ONLY)
        (sp / "zz_ipv4only.pth").write_text("import ipv4only\n")


def install_local_flux() -> dict[str, Any]:
    """Separate Python 3.12 environment with mflux (MLX), then one small picture to download the 4-bit model."""
    from .media import visuals
    py = shutil.which("python3.12") or "/opt/homebrew/bin/python3.12"
    venv = config.ROOT / ".venv-flux"
    steps = []
    if not visuals.local_flux_installed():
        subprocess.run([py, "-m", "venv", str(venv)], check=True, timeout=600)
        pip = str(venv / "bin" / "pip")
        subprocess.run([pip, "install", "-q", "--upgrade", "pip"], check=True, timeout=1200)
        subprocess.run([pip, "install", "-q", "mflux"], check=True, timeout=7200)
        _ipv4_only(venv)
        steps.append("software installed")
    if not visuals.local_flux_ready():
        import tempfile
        visuals.generate_local_flux("a simple empty room", "long", Path(tempfile.mkdtemp()) / "warmup.png",
                                    size=(512, 288))
        steps.append("model downloaded")
    logs.event("info", "setup", "Local FLUX ready: " + ", ".join(steps or ["already installed"]))
    return {"steps": steps}


def install_whisper() -> dict[str, Any]:
    from .editing import transcribe
    transcribe._load()
    return {"installed": "whisper"}


def install_voiceclone() -> dict[str, Any]:
    """Separate Python 3.12 environment with Chatterbox (its torch/numpy pins differ from the app's), then the model."""
    from .media import voiceclone
    py = shutil.which("python3.12") or "/opt/homebrew/bin/python3.12"
    venv = config.ROOT / ".venv-voiceclone"
    steps = []
    if not voiceclone.installed():
        subprocess.run([py, "-m", "venv", str(venv)], check=True, timeout=600)
        pip = str(venv / "bin" / "pip")
        subprocess.run([pip, "install", "-q", "--upgrade", "pip"], check=True, timeout=1200)
        subprocess.run([pip, "install", "-q", "chatterbox-tts", "setuptools<81"], check=True, timeout=7200)
        steps.append("software installed")
    if not voiceclone.model_ready():                 # first start downloads the ~6 GB model
        voiceclone._worker()
        steps.append("model downloaded")
    logs.event("info", "setup", "Voice cloning ready: " + ", ".join(steps or ["already installed"]))
    return {"steps": steps}


INSTALLERS = {"whisper_download": install_whisper, "voiceclone_setup": install_voiceclone,
              "local_flux_setup": install_local_flux}


def running() -> set[str]:
    return {r["type"] for r in db.q("SELECT type FROM tasks WHERE status IN ('pending','running') AND type IN "
                                    "('kokoro_download','whisper_download','voiceclone_setup','local_flux_setup')")}
