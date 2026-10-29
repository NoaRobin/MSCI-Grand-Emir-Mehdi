@echo off
REM Double-cliquer pour ouvrir l'interface d'extraction MSCI ESG.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Premiere utilisation : creation de l'environnement Python...
  python -m venv .venv || (echo Python 3.11+ introuvable. Installer Python ou contacter l'IT. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (echo Installation impossible : verifier le proxy avec l'IT. & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m app %*
pause
