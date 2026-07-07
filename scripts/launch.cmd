@echo off
rem Launcher de doble click para Windows (design.md 2.5): levanta el
rem contenedor si el servidor no responde y abre el browser. Crear un
rem acceso directo a este archivo en el escritorio.
rem Mensajes en ASCII a proposito: cmd.exe usa otra codepage que el repo.
setlocal
cd /d "%~dp0.."

set PORT=%FACTURADOR_PORT%
if "%PORT%"=="" set PORT=8399

curl -fsS -o NUL http://localhost:%PORT%/health 2>NUL
if %errorlevel%==0 goto abrir

echo Levantando facturador (docker compose up -d)...
docker compose up -d --build
if errorlevel 1 (
    echo ERROR: docker compose fallo. Esta corriendo Docker Desktop?
    pause
    exit /b 1
)

set /a intentos=0
:esperar
curl -fsS -o NUL http://localhost:%PORT%/health 2>NUL
if %errorlevel%==0 goto abrir
set /a intentos+=1
if %intentos% geq 60 (
    echo ERROR: el servidor no respondio en 60 s. Revisar: docker compose logs facturador
    pause
    exit /b 1
)
timeout /t 1 /nobreak >NUL
goto esperar

:abrir
start http://localhost:%PORT%/
endlocal
