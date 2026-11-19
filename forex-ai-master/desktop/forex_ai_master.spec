# PyInstaller spec — Forex AI Master (Windows).
# Builds two outputs from one analysis:
#   dist/ForexAIMaster.exe        portable single file
#   dist/ForexAIMaster/           application folder packaged by the installer (installer.iss)
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "assets", "forex_ai_master.ico")
hidden = [m for m in collect_submodules("backend") if ".tests" not in m]
hidden += collect_submodules("uvicorn") + collect_submodules("websockets")
hidden += ["sqlalchemy.dialects.sqlite", "bcrypt", "jwt", "webview"]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "frontend", "dist"), "frontend_dist"),
           (os.path.join(SPECPATH, "assets"), "assets")],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook", "psycopg", "alembic", "PIL"],
)
pyz = PYZ(a.pure)

portable = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="ForexAIMaster", icon=ICON, console=False, upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
)

app_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ForexAIMaster", icon=ICON, console=False, upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
    contents_directory="_internal",
)
COLLECT(app_exe, a.binaries, a.datas, name="ForexAIMaster", upx=False)
