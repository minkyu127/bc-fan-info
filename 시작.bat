@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 블랙컴뱃 팬 인포

if not exist ".venv\Scripts\python.exe" (
    echo [처음 실행] 필요한 프로그램을 설치하고 있어요. 1~2분 걸려요...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python을 찾을 수 없어요. python.org 에서 Python을 설치해 주세요.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)

echo.
echo  블랙컴뱃 팬 인포를 시작합니다.
echo  브라우저가 자동으로 열려요. 안 열리면 주소창에 http://127.0.0.1:5000 입력
echo  끝내려면 이 검은 창을 닫으면 돼요.
echo.
".venv\Scripts\python.exe" app.py
pause
