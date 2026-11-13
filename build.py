"""Gera o executável e o instalador para o sistema operacional atual.

    pip install -r requirements-build.txt
    python build.py

Windows -> dist/MP4AutoSlow.exe e dist/MP4AutoSlow-Setup.exe (requer Inno Setup 6)
macOS   -> dist/MP4AutoSlow.app, dist/MP4AutoSlow-macos.zip e dist/MP4AutoSlow.dmg

O PyInstaller não faz cross-compile: rode em cada sistema que quiser gerar.
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "MP4AutoSlow"
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def build_dmg(app: Path) -> Path:
    dmg = DIST / f"{NAME}.dmg"
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    subprocess.run(["ditto", str(app), str(staging / app.name)], check=True)
    (staging / "Applications").symlink_to("/Applications")
    dmg.unlink(missing_ok=True)
    subprocess.run(
        ["hdiutil", "create", "-volname", "MP4 Auto Slow", "-srcfolder", str(staging),
         "-ov", "-format", "UDZO", str(dmg)],
        check=True,
    )
    return dmg


def find_iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if not root:
            continue
        for sub in ("Inno Setup 6", "Programs/Inno Setup 6"):
            candidate = Path(root) / sub / "ISCC.exe"
            if candidate.exists():
                return str(candidate)
    return None


def main() -> None:
    system = platform.system()
    if system not in ("Windows", "Darwin"):
        print(f"Aviso: sistema {system} não é alvo oficial (Windows/macOS).")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--name", NAME,
        # inclui o binário do ffmpeg que acompanha o imageio-ffmpeg
        "--collect-all", "imageio_ffmpeg",
        str(ROOT / "app.py"),
    ]
    if system == "Windows":
        cmd.insert(-1, "--onefile")  # .exe único

    subprocess.run(cmd, check=True, cwd=ROOT)

    if system == "Darwin":
        app = DIST / f"{NAME}.app"
        zip_path = DIST / f"{NAME}-macos.zip"
        zip_path.unlink(missing_ok=True)
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(app), str(zip_path)], check=True
        )
        dmg = build_dmg(app)
        print(f"\nPronto:\n  {app}\n  {zip_path}\n  {dmg}")
    else:
        print(f"\nPronto: {DIST / (NAME + '.exe')}")
        iscc = find_iscc()
        if iscc:
            subprocess.run([iscc, str(ROOT / "installer" / "windows.iss")], check=True)
            print(f"  {DIST / (NAME + '-Setup.exe')}")
        else:
            print("Inno Setup 6 não encontrado: instalador não gerado "
                  "(https://jrsoftware.org/isdl.php).")

    shutil.rmtree(ROOT / "build", ignore_errors=True)


if __name__ == "__main__":
    main()
