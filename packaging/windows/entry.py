"""Script de entrada de PyInstaller: ``FacturadorE.exe``.

Toda la lógica vive en ``facturador.launcher.frozen`` (testeable sin
empaquetar); acá solo se delega.
"""

from facturador.launcher.frozen import main

if __name__ == "__main__":
    raise SystemExit(main())
