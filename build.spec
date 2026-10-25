# PyInstaller spec for Voice To Text Studio (PyQt6 desktop app).
#
# Build:
#   cd desktop_pyqt6
#   source .venv/bin/activate   (or your venv)
#   pyinstaller build.spec
#
# Output: dist/VoiceToTextStudio/ (onedir) and, on macOS, dist/VoiceToTextStudio.app
#
# Build on each target architecture (Apple Silicon or Intel). FFmpeg is
# discovered through Homebrew and included in the app bundle; espeak-ng still
# needs to be installed separately for VieNeu TTS.

import os
import shutil
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("app/resources", "app/resources")]
binaries = []
hiddenimports = []

for tool in ("ffmpeg", "ffprobe"):
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"{tool} is required to build; install it with: brew install ffmpeg")
    binaries.append((path, "."))

# faster-whisper (STT) and argostranslate (offline translation) pull in native
# extensions / data files that PyInstaller's static analysis won't find on its
# own — collect them explicitly. Each is optional at runtime (only needed the
# first time a video/audio job is transcribed or translated), so skip quietly
# if a package isn't installed in this venv.
for pkg in ("faster_whisper", "ctranslate2", "vieneu", "argostranslate", "stanza", "sentencepiece", "tokenizers"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceToTextStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VoiceToTextStudio",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="VoiceToTextStudio.app",
        icon=None,
        bundle_identifier="com.voicetextstudio.desktop",
    )
