# PyInstaller spec — build with:  .venv\Scripts\pyinstaller Tintr.spec --noconfirm
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("pytvpaint") + collect_submodules("keyring.backends") + ["win32ctypes.core"]
excl = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf", "tkinter", "matplotlib"]

a = Analysis(["run.py"], pathex=["."], hiddenimports=hidden, excludes=excl)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Tintr", console=False, upx=False,
          runtime_tmpdir=None)
