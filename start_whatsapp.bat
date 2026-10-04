@echo off
cd /d "%~dp0"
rem L'ancien tunnel (hors Docker) ne doit plus tourner : le tunnel est dans Docker
taskkill /F /T /FI "WINDOWTITLE eq MPG - *" >nul 2>&1
taskkill /F /IM cloudflared.exe >nul 2>&1

rem Docker Desktop doit tourner : on le lance s'il est arrete
docker info >nul 2>&1
if not errorlevel 1 goto docker_pret
echo Demarrage de Docker Desktop (1 a 2 minutes)...
start "" "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
set /a essais=0
:attente_docker
timeout /t 3 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto docker_pret
set /a essais+=1
if %essais% lss 60 goto attente_docker
echo [ERREUR] Docker Desktop ne demarre pas. Ouvrez-le a la main puis relancez ce fichier.
pause
exit /b 1

:docker_pret
rem Site + base de donnees + tunnel + enregistrement chez Meta, tout dans Docker.
rem --build : reconstruit l'image si le code a change (rapide sinon)
echo Demarrage de l'application dans Docker...
docker compose up -d --build
if errorlevel 1 (
    echo [ERREUR] L'application n'a pas demarre dans Docker.
    pause
    exit /b 1
)
echo Enregistrement de l'adresse du tunnel chez Meta...
timeout /t 25 /nobreak >nul
docker compose logs register --tail 5
echo.
pause
