"""
EMERGENCY: Удаляет ВСЕ лобби на Steam аккаунтах
Использовать когда лобби висят после краша бота
"""

import json
import time
import logging
import gevent
from steam.client import SteamClient
from steam.enums import EResult
from dota2.client import Dota2Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def destroy_all_lobbies_for_account(username: str, password: str):
    """Заходит на аккаунт и удаляет все лобби"""
    try:
        logger.info(f"[{username}] Подключение к Steam...")
        
        steam = SteamClient()
        dota = Dota2Client(steam)
        
        ready = False
        
        def on_ready():
            nonlocal ready
            ready = True
            logger.info(f"[{username}] Dota 2 готов")
        
        dota.on('ready', on_ready)
        
        # Логин
        result = steam.login(username=username, password=password)
        if result != EResult.OK:
            logger.error(f"[{username}] Ошибка входа: {result}")
            return False
        
        logger.info(f"[{username}] Успешный вход")
        
        # Запуск Dota
        dota.launch()
        
        # Ждём готовности
        for i in range(30):
            gevent.sleep(1)
            if ready:
                break
        
        if not ready:
            logger.error(f"[{username}] Dota не готов")
            return False
        
        # Дополнительная пауза
        gevent.sleep(5)
        
        # УДАЛЯЕМ ЛОББИ
        logger.info(f"[{username}] Удаление лобби...")
        try:
            dota.destroy_lobby()
            gevent.sleep(2)
            logger.info(f"[{username}] ✅ Лобби удалено!")
        except Exception as e:
            logger.warning(f"[{username}] Ошибка удаления: {e}")
        
        # Выходим
        try:
            dota.leave_practice_lobby()
            gevent.sleep(1)
        except:
            pass
        
        steam.disconnect()
        logger.info(f"[{username}] Отключились")
        return True
        
    except Exception as e:
        logger.error(f"[{username}] ОШИБКА: {e}", exc_info=True)
        return False

def main():
    # Загружаем аккаунты
    try:
        with open('steam_accounts.json', 'r') as f:
            accounts = json.load(f)
    except Exception as e:
        logger.error(f"Не удалось загрузить steam_accounts.json: {e}")
        return
    
    logger.info(f"Загружено {len(accounts)} аккаунтов")
    logger.info("=" * 50)
    logger.info("🔥 УДАЛЕНИЕ ВСЕХ ЛОББИ")
    logger.info("=" * 50)
    
    for idx, acc in enumerate(accounts, 1):
        username = acc['username']
        password = acc['password']
        
        logger.info(f"\n[{idx}/{len(accounts)}] Обработка {username}...")
        
        success = destroy_all_lobbies_for_account(username, password)
        
        if success:
            logger.info(f"✅ {username} - готово")
        else:
            logger.error(f"❌ {username} - ошибка")
        
        # Пауза между аккаунтами
        if idx < len(accounts):
            logger.info("Пауза 5 секунд...")
            time.sleep(5)
    
    logger.info("\n" + "=" * 50)
    logger.info("✅ ВСЕ АККАУНТЫ ОБРАБОТАНЫ!")
    logger.info("=" * 50)

if __name__ == "__main__":
    main()

