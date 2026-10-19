"""Build a native one-directory release using a selected build environment."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paths import __version__


def distribution_licenses(name: str) -> str:
    dist = importlib.metadata.distribution(name)
    files = [p for p in dist.files or [] if p.name.lower().startswith(("license", "copying"))]
    if not files:
        raise RuntimeError(f"No license text found in the {name} build distribution")
    return "\n\n".join(dist.locate_file(p).read_text(encoding="utf-8") for p in sorted(files))


def write_notices() -> Path:
    candidates = [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE",
                  Path(sysconfig_stdlib()) / "LICENSE.txt"]
    license_path = next((p for p in candidates if p.is_file()), None)
    if license_path is None:
        # Some Linux distributions retain PSF's license in the stdlib documentation.
        import pydoc_data
        license_path = Path(pydoc_data.__file__).parent.parent / "LICENSE.txt"
    if not license_path.is_file():
        raise RuntimeError("Python PSF license text not found in the build environment")
    text = ("THIRD-PARTY NOTICES\n\nPython " + platform.python_version() + " (PSF license)\n\n"
            + license_path.read_text(encoding="utf-8")
            + "\n\nPillow " + importlib.metadata.version("Pillow") + " (MIT-CMU / HPND and bundled libraries)\n\n"
            + distribution_licenses("Pillow")
            + "\n\nPyInstaller " + importlib.metadata.version("PyInstaller")
            + "\nThe PyInstaller bootloader exception permits distributing bundled applications "
              "under their own licenses. PyInstaller is used only at build time. "
              "Its license and exception text follow.\n\n"
            + distribution_licenses("PyInstaller"))
    target = ROOT / "build" / "THIRD-PARTY-NOTICES.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def sysconfig_stdlib() -> str:
    import sysconfig
    return sysconfig.get_path("stdlib")


def build() -> Path:
    import PyInstaller
    if PyInstaller.__version__ != "6.22.3":
        raise RuntimeError("Install the pinned requirements-build.txt before building")
    notices = write_notices()
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build" / "pyinstaller-cache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    temp = ROOT / "build" / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env.update(TMP=str(temp), TEMP=str(temp), TMPDIR=str(temp))
    subprocess.run([sys.executable, "-B", "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
                    str(ROOT / "packaging" / "local-dream-desktop.spec")], cwd=ROOT, env=env, check=True)
    folder = ROOT / "dist" / "LocalDreamDesktop"
    shutil.copy2(notices, folder / notices.name)
    shutil.copy2(ROOT / "LICENSE.md", folder / "LICENSE.md")
    shutil.copy2(ROOT / "vendor" / "LICENSE-qrcode", folder / "LICENSE-qrcode")
    shutil.copy2(ROOT / "packaging" / "README.md", folder / "PACKAGING.md")
    # Keep the optional skill installer self-contained in the portable folder.
    for relative in ("tools/install_skill.py", ".claude/skills/local-dream/SKILL.md"):
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    if sys.platform != "win32":
        launcher = folder / "local-dream-desktop.sh"
        shutil.copy2(ROOT / "local-dream-desktop.sh", launcher)
        launcher.chmod(0o755)
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform)
    if system is None:
        raise RuntimeError("Unsupported build platform")
    machine = (platform.machine() or sysconfig.get_platform().rsplit("-", 1)[-1]).lower()
    arch = "x64" if machine in ("amd64", "x86_64") else "arm64" if machine in ("arm64", "aarch64") else machine
    archive = ROOT / "dist" / f"LocalDreamDesktop-{__version__}-{system}-{arch}"
    result = Path(shutil.make_archive(str(archive), "zip" if system == "windows" else "gztar",
                                     root_dir=folder.parent, base_dir=folder.name))
    print(f"Built {result.name}: {result.stat().st_size} bytes", flush=True)
    if system == "macos":
        print("macOS: UNTESTED unsigned candidate; see PACKAGING.md for Gatekeeper notes.")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python executable in the build venv")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.worker:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.call([args.python, "-B", str(Path(__file__).resolve()), "--worker"], cwd=ROOT, env=env)
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
