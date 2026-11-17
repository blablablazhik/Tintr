# PyInstaller spec — build with:  .venv\Scripts\pyinstaller Tintr.spec --noconfirm
from PyInstaller.utils.hooks import collect_submodules

hidden = (collect_submodules("pytvpaint") + collect_submodules("keyring.backends")
          + ["win32ctypes.core", "clr", "webview.platforms.winforms", "webview.platforms.edgechromium"])
excl = ["PySide6", "PyQt5", "PyQt6", "tkinter", "matplotlib", "PyInstaller"]

a = Analysis(["run.py"], pathex=["."], hiddenimports=hidden, excludes=excl,
             datas=[("tintr/web/static", "tintr/web/static")])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Tintr", console=False, upx=False,
          runtime_tmpdir=None, icon="tintr/web/static/tintr.ico")
