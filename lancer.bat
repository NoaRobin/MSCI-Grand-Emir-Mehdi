@echo off
REM ==========================================================================
REM  Extraction MSCI ESG - lanceur Windows. Double-cliquer sur ce fichier.
REM
REM  1. Cherche un Python 3.10+ : copie locale (.python), puis "py", puis "python".
REM  2. Si aucun n'est trouve, propose de telecharger le Python officiel
REM     (python.org, version "embeddable") dans le dossier .python du projet :
REM     aucun droit administrateur, rien n'est installe sur le poste.
REM  3. Lance l'application ; le navigateur s'ouvre tout seul.
REM  Les bibliotheques sont fournies dans vendor\ : pas de pip, pas d'internet.
REM ==========================================================================
setlocal EnableExtensions
title Extraction MSCI ESG
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "PY_VERSION=3.12.10"

REM pushd gere aussi les dossiers reseau (\\serveur\partage).
pushd "%~dp0" 2>nul || goto :no_folder

if not exist "app\__main__.py" (
  echo Le dossier "app" est introuvable a cote de lancer.bat.
  echo Si vous avez ouvert le fichier depuis un .zip : faites d'abord un clic droit
  echo sur le .zip, "Extraire tout...", puis lancez lancer.bat depuis le dossier extrait.
  goto :fail
)

call :find_python
if not defined PY call :install_python
if not defined PY goto :fail

echo.
echo Demarrage de l'application (Python : %PY%)...
echo Le navigateur va s'ouvrir. Gardez cette fenetre ouverte pendant l'utilisation.
echo Pour arreter : fermez cette fenetre.
echo.
%PY% -m app %*
if errorlevel 1 (
  echo.
  echo L'application s'est arretee sur une erreur ^(message ci-dessus^).
  echo Voir la partie "En cas de probleme" du guide : docs\Guide_utilisateur.pdf
  goto :fail
)
popd
exit /b 0


:find_python
set "PY="
if exist ".python\python.exe" (
  ".python\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=.python\python.exe"
)
if defined PY exit /b 0
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY exit /b 0
REM Le faux "python" du Microsoft Store echoue a ce test : il est donc ignore.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
exit /b 0


:install_python
echo.
echo Aucun Python 3.10 ou plus recent n'a ete trouve sur ce poste.
echo.
echo Le lanceur peut telecharger le Python officiel %PY_VERSION% (python.org, environ 11 Mo)
echo dans le sous-dossier .python de ce projet. Rien n'est installe sur Windows et
echo aucun droit administrateur n'est demande. A faire une seule fois.
echo.
choice /C ON /M "Telecharger Python maintenant ? (O = oui, N = non)"
if errorlevel 2 (
  echo.
  echo D'accord. Installez Python 3.10+ depuis le Centre logiciel de l'entreprise ou
  echo https://www.python.org/downloads/ puis relancez lancer.bat.
  exit /b 1
)
echo.
echo Telechargement en cours...
if exist ".python" rmdir /s /q ".python"
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; [Net.WebRequest]::DefaultWebProxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials; $arch = switch ($env:PROCESSOR_ARCHITECTURE) { 'ARM64' { 'arm64' } 'x86' { 'win32' } default { 'amd64' } }; $url = 'https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-' + $arch + '.zip'; Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile 'python-embed.zip'; Expand-Archive -Force -Path 'python-embed.zip' -DestinationPath '.python'; Remove-Item 'python-embed.zip'; Get-ChildItem '.python\*._pth' | ForEach-Object { Add-Content -LiteralPath $_.FullName -Value '..' }"
if errorlevel 1 (
  if exist "python-embed.zip" del /q "python-embed.zip"
  echo.
  echo Le telechargement a echoue ^(proxy, pare-feu ou poste sans internet^).
  echo Demandez a l'IT d'installer Python 3.10+ ^(Centre logiciel^), puis relancez lancer.bat.
  exit /b 1
)
call :find_python
if not defined PY (
  echo Python a ete telecharge mais ne demarre pas. Contactez l'IT.
  exit /b 1
)
echo Python installe dans le dossier .python du projet.
exit /b 0


:no_folder
echo Impossible d'ouvrir le dossier du projet : "%~dp0"

:fail
echo.
pause
popd 2>nul
exit /b 1
