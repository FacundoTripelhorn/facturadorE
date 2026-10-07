; Instalador de FacturadorE para Windows (Inno Setup 6.3 o posterior).
;
; Lo compila build.ps1 después de PyInstaller:
;   ISCC /DAppVersion=<versión> packaging\windows\FacturadorE.iss
; Deja packaging\windows\dist\FacturadorE-Setup-<versión>.exe.
;
; Por usuario y sin administrador: instala en %LOCALAPPDATA%\Programs\FacturadorE.
; Los datos (perfiles, certificados, base) viven en %LOCALAPPDATA%\FacturadorE
; y ni el instalador ni el desinstalador los tocan: acá no hay ninguna
; sección que escriba o borre fuera de {app} y del acceso directo.

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
; Identidad de la instalación: no cambiar, o la versión nueva se instala al
; lado de la anterior en vez de actualizarla.
AppId={{0BE8B74A-F02D-4187-ACB8-CD7795DC2F84}
AppName=FacturadorE
AppVersion={#AppVersion}
AppVerName=FacturadorE {#AppVersion}
UninstallDisplayName=FacturadorE
UninstallDisplayIcon={app}\FacturadorE.exe
PrivilegesRequired=lowest
DefaultDirName={autopf}\FacturadorE
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=build\facturadore.ico
OutputDir=dist
OutputBaseFilename=FacturadorE-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
; Actualizar es instalar encima: cierra la app si está abierta (primero le
; pide a la ventana que cierre, que apaga el backend) y no la reabre sola.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[InstallDelete]
; _internal se reemplaza completo: no quedan DLL de la versión anterior.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\FacturadorE\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\FacturadorE"; Filename: "{app}\FacturadorE.exe"

[Run]
Filename: "{app}\FacturadorE.exe"; Description: "{cm:LaunchProgram,FacturadorE}"; Flags: nowait postinstall skipifsilent
