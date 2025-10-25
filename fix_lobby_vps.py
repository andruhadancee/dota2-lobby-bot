#!/usr/bin/env python3
"""
Скрипт для замены метода create_lobby на правильную версию с tournament_game
"""
import re

file_path = '/root/dota2_lobby_bot/dota2_real_lobby_bot_v2.py'

new_create_lobby = '''    def create_lobby(self, lobby_name: str, password: str, server: str, mode: str) -> Optional[dict]:
        try:
            logger.info(f"[{self.username}] 🎮 Создание лобби: {lobby_name}")
            
            server_region = self._get_server_region(server)
            game_mode = self._get_game_mode(mode)
            
            # Создаём БЕЗ параметров
            self.dota.create_practice_lobby()
            
            if self.lobby_created_event.wait(timeout=30):
                # СРАЗУ выходим из команды (освобождаем слот)
                try:
                    self.dota.practice_lobby_kick_from_team(self.steam.steam_id.as_32)
                    logger.info(f"[{self.username}] ⬅️ Вышел из команды")
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"[{self.username}] ⚠️ Не удалось выйти из команды: {e}")
                
                # СРАЗУ идём в канал трансляции (НЕ занимаем слот игрока!)
                try:
                    self.dota.join_practice_lobby_broadcast_channel(channel=1)
                    logger.info(f"[{self.username}] 📺 Присоединился к каналу трансляции")
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"[{self.username}] ⚠️ Не удалось присоединиться к трансляции: {e}")
                
                # ТЕПЕРЬ настраиваем (включая турнир)
                options = {
                    'game_name': lobby_name,
                    'pass_key': password,
                    'server_region': server_region,
                    'game_mode': game_mode,
                    'selection_priority_rules': 1,
                    'allow_spectating': False,
                    'allow_cheats': False,
                    'fill_with_bots': False,
                    'tournament_game': True,
                    'leagueid': 18390,
                }
                
                self.dota.config_practice_lobby(options=options)
                logger.info(f"[{self.username}] ⚙️ Настройки применены")
                time.sleep(2)
                
                logger.info(f"[{self.username}] ✅ Лобби создано!")
                
                lobby_info = {
                    'lobby_name': lobby_name,
                    'password': password,
                    'account': self.username,
                    'server': server,
                    'mode': mode
                }
                return lobby_info
            else:
                logger.error(f"[{self.username}] ❌ Таймаут создания лобби")
                return None
        except Exception as e:
            logger.error(f"[{self.username}] ❌ Ошибка создания лобби: {e}", exc_info=True)
            return None
'''

print("📖 Читаю файл...")
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

print("🔍 Ищу метод create_lobby...")
# Находим и заменяем весь метод create_lobby
pattern = r'    def create_lobby\(self, lobby_name: str.*?(?=\n    def )'
match = re.search(pattern, content, flags=re.DOTALL)

if match:
    print(f"✅ Найден метод create_lobby (строки {content[:match.start()].count(chr(10))+1} - {content[:match.end()].count(chr(10))+1})")
    content = content[:match.start()] + new_create_lobby + '\n' + content[match.end():]
    
    print("💾 Сохраняю изменения...")
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("✅ ВСЁ ГОТОВО!")
    print("\n🔍 Проверяем:")
    import subprocess
    result = subprocess.run(['grep', '-A', '3', 'tournament_game', file_path], 
                          capture_output=True, text=True)
    print(result.stdout)
else:
    print("❌ Метод create_lobby не найден!")


