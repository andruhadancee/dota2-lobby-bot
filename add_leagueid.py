#!/usr/bin/env python3
"""
Скрипт для добавления leagueid в options на VPS
"""

script = """
sed -i '/options = {/,/}/ {
    /}/ i\\
                '\''leagueid'\'': 18390,
}' ~/dota2_lobby_bot/dota2_real_lobby_bot_v2.py
"""

print("=== КОМАНДЫ ДЛЯ VPS ===\n")
print("# 1. Остановить бота (Ctrl+C в screen)")
print("screen -r dota2bot")
print("# Нажмите Ctrl+C, затем Ctrl+A+D\n")

print("# 2. Добавить leagueid в options:")
print("cd ~/dota2_lobby_bot")
print("sed -i \"/options = {/,/}/ s/'tournament_game': True/'tournament_game': True,\\n                'leagueid': 18390/\" dota2_real_lobby_bot_v2.py")
print()

print("# 3. Проверить изменения:")
print("grep -A 12 'options = {' dota2_real_lobby_bot_v2.py | grep -A 12 'config_practice_lobby'")
print()

print("# 4. Перезапустить бота:")
print("screen -r dota2bot")
print("python3 main.py")
print("# Нажмите Ctrl+A+D для выхода")


