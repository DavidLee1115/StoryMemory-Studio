@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0"

echo.
echo ============================================================
echo  StoryMemory Studio - Ollama Batch Novel Writer
echo ============================================================
echo.
echo  The Chinese interactive questions are shown by Python.
echo  This BAT only starts the StoryMemory + Ollama workflow.
echo.

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" scripts\ollama_story_batch.py %*
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
    echo Done. Please check the exports folder.
) else (
    echo Failed with exit code: %EXITCODE%
    echo Please check Python, requirements.txt, Ollama service, and local models.
    echo Suggested model: ollama pull qwen3
)
echo.
pause
exit /b %EXITCODE%
