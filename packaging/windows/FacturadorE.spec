# PyInstaller spec del build de Windows: una carpeta con FacturadorE.exe.
#
# Lo corre build.ps1 (local y CI) después de dejar en build/ el ícono y
# age.exe / age-keygen.exe. One-folder a propósito: arranca más rápido que
# one-file (no se descomprime en cada inicio) y dispara menos falsos
# positivos de antivirus.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)
BUILD = HERE / "build"

datas = collect_data_files("facturador")  # templates, static, fonts, schema.sql
datas += [(str(BUILD / "facturadore.ico"), ".")]
# Versión del build (la escribe build.ps1) para la ventana de error.
datas += [(str(BUILD / "version.txt"), ".")]
datas += [(str(p), "bin") for p in sorted((BUILD / "bin").iterdir())]

hiddenimports = collect_submodules("uvicorn") + [
    "tkinter",
    "tkinter.font",
    # Ventanas del launcher: formas y marca dibujadas con Pillow.
    "PIL.ImageTk",
]

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(HERE.parent.parent)],
    datas=datas,
    hiddenimports=hiddenimports,
    # Solo tests/dev: no viajan en el bundle.
    excludes=["pytest", "mypy", "ruff", "pypdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FacturadorE",
    icon=str(BUILD / "facturadore.ico"),
    # App de ventana: sin consola. Los errores se muestran en diálogo y
    # la salida va a launcher.log (facturador.launcher.frozen).
    console=False,
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="FacturadorE",
)
