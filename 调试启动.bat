@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PATH=%ROOT%python;%ROOT%python\Scripts;%PATH%"
set "PYTHONMALLOC=malloc"
cd /d "%ROOT%scraper"
echo Starting scraper...
echo If it crashes, error will be shown below:
echo.
"%ROOT%python\python.exe" "%ROOT%scraper\main.py"
echo.
echo Exit code: %ERRORLEVEL%
pause
