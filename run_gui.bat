@echo off
cd /d %~dp0
py tategakiXTC_gui_studio.py
if errorlevel 1 (
  echo.
  echo 起動に失敗しました。上に表示されたメッセージを確認してください。
)
pause
