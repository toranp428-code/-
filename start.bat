@echo off
chcp 65001 >nul
set /p DISCORD_TOKEN=Discord Bot Tokenを入力してください（画面に表示されます）:
python bot.py
pause
