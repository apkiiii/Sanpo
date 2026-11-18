# PyInstaller spec — builds ForexAIMaster.exe (run on Windows: pyinstaller desktop/forex_ai_master.spec)
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = [m for m in collect_submodules("backend") if ".tests" not in m]
hidden += collect_submodules("uvicorn") + collect_submodules("websockets")
hidden += ["sqlalchemy.dialects.sqlite", "bcrypt", "jwt"]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "frontend", "dist"), "frontend_dist")],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook", "psycopg", "alembic"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="ForexAIMaster",
    console=True,
    upx=False,
)
