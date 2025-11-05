@echo off
chcp 65001 >nul
cd /d "C:\Users\lisag\Desktop\бот для создания лобби"
git add dota2_real_lobby_bot_v2.py
git commit -m "fix: use gevent.event.Event + remove captain check (not needed in Dota 2)"
git push
pause

