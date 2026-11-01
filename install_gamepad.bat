@echo off
chcp 65001 >nul
rem Joy-Con を PC の仮想 Xbox コントローラーとして使うための vgamepad と ViGEmBus ドライバーを導入します。
rem ドライバーのインストーラーが表示されたら画面の指示に従ってください。
cd /d "%~dp0"
python -m pip install vgamepad
python -c "import vgamepad; vgamepad.VX360Gamepad(); print(\"OK: 仮想 Xbox コントローラーが使えます\")"
pause
