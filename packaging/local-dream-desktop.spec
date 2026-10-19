# One Analysis / PYZ, two entry executables, one shared _internal directory.
from pathlib import Path

root = Path(SPECPATH).parent
data = [(str(root / name), ".") for name in
        ("studio.html", "LICENSE.md", "build/THIRD-PARTY-NOTICES.txt")]
data += [(str(root / "vendor"), "vendor"),
         (str(root / "assets/icon.png"), "assets")]
a = Analysis([str(root / "local_dream_desktop.py")], pathex=[str(root)],
             binaries=[], datas=data, hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["tkinter", "pytest", "unittest", "tests"], noarchive=False)
pyz = PYZ(a.pure)
common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, icon=str(root / "local-dream-desktop.ico"))
gui = EXE(pyz, a.scripts, [], name="Local Dream Desktop", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="ldd", console=True, **common)
app = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False,
              name="LocalDreamDesktop")
