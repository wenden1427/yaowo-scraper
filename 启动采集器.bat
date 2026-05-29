@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PATH=%ROOT%python;%ROOT%python\Scripts;%PATH%"
set "PYTHONMALLOC=malloc"
start "" "%ROOT%python\pythonw.exe" "%ROOT%scraper\gui.py"
