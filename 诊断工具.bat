@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 采集器诊断工具

set "ROOT=%~dp0"
set "PYTHON=%ROOT%python\python.exe"
set "SCRAPER=%ROOT%scraper"

echo =============================================
echo   采集器诊断工具
echo =============================================
echo.

:: 1. Check Python
echo [1/6] 检查Python环境...
if not exist "%PYTHON%" (
    echo   [失败] 找不到 python.exe
    echo   路径: %PYTHON%
    goto :end
)
echo   [OK] Python: %PYTHON%

:: 2. Check DLLs
echo [2/6] 检查运行时依赖...
set "DLLS=%ROOT%python\DLLs"
set MISSING_DLL=0
for %%d in (vcruntime140.dll vcruntime140_1.dll python314.dll _tkinter.pyd unicodedata.pyd tk86t.dll tcl86t.dll) do (
    if not exist "%DLLS%\%%d" (
        echo   [缺失] %%d
        set MISSING_DLL=1
    )
)
if !MISSING_DLL!==1 (
    echo   [警告] 缺少运行时文件，程序可能无法启动
) else (
    echo   [OK] 运行时DLL完整
)

:: 3. Check cloakbrowser
echo [3/6] 检查浏览器...
set "CLOAK=%ROOT%cloakbrowser\chrome.exe"
if not exist "%CLOAK%" (
    echo   [缺失] cloakbrowser\chrome.exe
) else (
    echo   [OK] cloakbrowser: !CLOAK!
)

:: 4. Python import test
echo [4/6] 检查Python依赖包...
set "PATH=%ROOT%python;%ROOT%python\DLLs;%ROOT%python\Scripts;%PATH%"
set PYTHONMALLOC=malloc
cd /d "%SCRAPER%"
"%PYTHON%" -c "
import sys
errors=[]
tests=[
    ('tkinter','tkinter'),
    ('_tkinter','_tkinter'),
    ('unicodedata','unicodedata'),
    ('bs4','BeautifulSoup'),
    ('playwright','playwright'),
    ('requests','requests'),
    ('PIL','Pillow'),
    ('openpyxl','openpyxl'),
    ('dotenv','python-dotenv'),
    ('colorama','colorama'),
]
for mod,pkg in tests:
    try:
        __import__(mod)
        print(f'  [OK] {pkg}')
    except Exception as e:
        print(f'  [缺失] {pkg} - {e}')
        errors.append(pkg)
if errors:
    print(f'\n  共{len(errors)}个依赖缺失，请安装: pip install {\" \".join(errors)}')
else:
    print(f'\n  [OK] 所有依赖完整')
" 2>&1

:: 5. Check error log
echo [5/6] 检查运行错误...
set "ERRLOG=%SCRAPER%\error.log"
if exist "%ERRLOG%" (
    echo   [发现] 存在错误日志:
    echo   ----------------------------------------
    type "%ERRLOG%"
    echo   ----------------------------------------
    echo.
    choice /c yn /m "是否清除错误日志"
    if !errorlevel!==2 goto :skip_clear
    del "%ERRLOG%"
    echo   [已清除]
) else (
    echo   [OK] 无错误日志
)
:skip_clear

:: 6. Launch test
echo [6/6] 尝试启动GUI...
echo   正在启动采集器（等待5秒检测崩溃）...
start "" "%ROOT%python\pythonw.exe" "%SCRAPER%\gui.py"
timeout /t 5 /nobreak >nul

:: Check if process is still running
tasklist /fi "imagename eq pythonw.exe" 2>nul | find "pythonw.exe" >nul
if !errorlevel!==0 (
    echo   [OK] 采集器正在运行
    echo.
    echo =============================================
    echo   诊断完成！采集器已启动
    echo =============================================
) else (
    echo   [异常] 采集器已退出！
    if exist "%ERRLOG%" (
        echo   --- 错误详情 ---
        type "%ERRLOG%"
        echo   ---
    )
    echo.
    echo   可能原因：
    echo   1. 显卡驱动问题 - 尝试更新显卡驱动
    echo   2. 杀毒软件拦截 - 将采集器目录加入白名单
    echo   3. Windows版本过旧 - 需要Win10 64位以上
)

:end
echo.
echo 按任意键关闭...
pause >nul
