"""
Dota 2 Real Lobby Bot v2 - УЛУЧШЕННАЯ ВЕРСИЯ
- Управление ботами (удаление, редактирование)
- Выбор ботов для создания лобби
- Правильные названия: "wb cup 1", "wb cup 2"
"""

import os
import logging
import random
import string
import json
import time
import threading
import asyncio
import multiprocessing
import signal
from multiprocessing import Process, Queue
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dotenv import load_dotenv
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Steam и Dota 2
import gevent
from steam.client import SteamClient
from steam.enums import EResult
from dota2.client import Dota2Client
from dota2.enums import DOTA_GameMode, EServerRegion

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('dota2_real_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Состояния
(WAITING_LOBBY_COUNT, WAITING_ACCOUNT_DATA, WAITING_START_CODE, 
 WAITING_LOBBY_NAME, WAITING_SELECT_BOTS, WAITING_EDIT_BOT_DATA,
 WAITING_DELETE_CONFIRM, WAITING_GAME_MODE, WAITING_SERIES_TYPE,
 WAITING_MATCH_TEAM1, WAITING_MATCH_TEAM2, WAITING_MATCH_DATE,
 WAITING_MATCH_TIME, WAITING_MATCH_BO, WAITING_MATCH_GAME_MODE,
 WAITING_MATCH_SERIES, WAITING_MATCH_LIST) = range(17)


def steam_worker_process(username: str, password: str, lobby_name: str, 
                         lobby_password: str, server: str, mode: str, series_type: str,
                         result_queue: Queue, shutdown_event):
    """
    Функция для запуска в отдельном процессе.
    Выполняет вход в Steam, запуск Dota 2 и создание лобби.
    shutdown_event - для корректного удаления лобби перед выходом.
    series_type - тип серии: "bo1", "bo2", "bo3", "bo5"
    Автозапуск:
      - 1v1 Solo Mid: при 2 игроках (1 vs 1)
      - Остальные режимы: при 10 игроках (5 vs 5)
    """
    # НЕ используем monkey.patch_all() - это вызывает RecursionError
    # gevent работает и без этого в отдельном процессе
    
    logging.basicConfig(level=logging.INFO)
    local_logger = logging.getLogger(f"steam_worker_{username}")
    
    try:
        local_logger.info(f"[{username}] Процесс запущен")
        
        # Создаем Steam клиент
        steam = SteamClient()
        dota = Dota2Client(steam)
        
        lobby_created = gevent.event.Event()
        lobby_data_container = {'data': None}
        dota_ready = gevent.event.Event()
        
        def on_dota_ready():
            local_logger.info(f"[{username}] Dota 2 готов")
            dota_ready.set()  # Устанавливаем флаг готовности
        
        def on_lobby_created(lobby):
            local_logger.info(f"[{username}] Лобби создано!")
            lobby_data_container['data'] = lobby
            lobby_created.set()
        
        # Счётчик игроков для отслеживания изменений
        player_counts = {'last_count': 0, 'last_radiant': 0, 'last_dire': 0}
        
        def on_lobby_changed(lobby_obj):
            """Отслеживаем ВСЕ изменения в лобби (игроки, чат и т.д.)"""
            try:
                # Проверяем количество игроков
                if dota.lobby and hasattr(dota.lobby, 'all_members'):
                    radiant = sum(1 for m in dota.lobby.all_members if m.team == 0)
                    dire = sum(1 for m in dota.lobby.all_members if m.team == 1)
                    total = radiant + dire
                    
                    # Логируем ТОЛЬКО при изменениях
                    if (total != player_counts['last_count'] or 
                        radiant != player_counts['last_radiant'] or 
                        dire != player_counts['last_dire']):
                        
                        local_logger.info(f"[{username}] 👥 Игроков изменилось: {total}/10 (Radiant: {radiant}, Dire: {dire})")
                        
                        player_counts['last_count'] = total
                        player_counts['last_radiant'] = radiant
                        player_counts['last_dire'] = dire
                
            except Exception as e:
                pass  # Не спамим
        
        # Подписываемся на события
        dota.on('ready', on_dota_ready)
        dota.on(dota.EVENT_LOBBY_NEW, on_lobby_created)
        dota.on(dota.EVENT_LOBBY_CHANGED, on_lobby_changed)  # ВАЖНО: отслеживаем ВСЕ изменения
        
        # 1. Вход в Steam
        local_logger.info(f"[{username}] Подключение к Steam...")
        result = steam.login(username=username, password=password)
        
        if result != EResult.OK:
            local_logger.error(f"[{username}] Ошибка входа: {result}")
            result_queue.put({'success': False, 'error': f'Login failed: {result}'})
            return
        
        local_logger.info(f"[{username}] Успешный вход в Steam")
        
        # 2. Запуск Dota 2
        local_logger.info(f"[{username}] Запуск Dota 2...")
        dota.launch()
        
        # Ждем подключения к координатору (макс 60 сек) - используем событие вместо фиксированного времени
        if not dota_ready.wait(timeout=60):
            local_logger.error(f"[{username}] Таймаут подключения Dota 2")
            result_queue.put({'success': False, 'error': 'Dota 2 connection timeout'})
            return
        
        # КРИТИЧНО: Агрессивная очистка ВСЕХ старых турнирных лобби
        local_logger.info(f"[{username}] 🧹 Очистка старых турнирных лобби...")
        try:
            dota.leave_practice_lobby()
        except:
            pass
        
        try:
            dota.destroy_lobby()
        except:
            pass
        
        local_logger.info(f"[{username}] ✅ Очистка завершена, готовы создать новое лобби")
        
        # 3. Создание лобби
        local_logger.info(f"[{username}] Создание лобби: {lobby_name}")
        
        server_mapping = {
            'Stockholm': 8,  # Stockholm = регион 8 в Dota 2
            'Europe West': EServerRegion.Europe,
            'Russia': EServerRegion.Europe,
            'US East': EServerRegion.USEast,
            'US West': EServerRegion.USWest,
        }
        
        mode_mapping = {
            'Captains Mode': DOTA_GameMode.DOTA_GAMEMODE_CM,
            'All Pick': DOTA_GameMode.DOTA_GAMEMODE_AP,
            'Captains Draft': DOTA_GameMode.DOTA_GAMEMODE_CD,
            'Mid Only': DOTA_GameMode.DOTA_GAMEMODE_MO,
            '1v1 Solo Mid': DOTA_GameMode.DOTA_GAMEMODE_1V1MID,
            'Random Draft': DOTA_GameMode.DOTA_GAMEMODE_RD,
            'Single Draft': DOTA_GameMode.DOTA_GAMEMODE_SD,
        }
        
        # Маппинг серий игр
        series_mapping = {
            'bo1': 0,  # Best of 1 (одна игра)
            'bo2': 1,  # Best of 2 (две игры)
            'bo3': 2,  # Best of 3 (до 2 побед)
            'bo5': 3,  # Best of 5 (до 3 побед)
        }
        
        server_region = server_mapping.get(server, EServerRegion.Europe)
        game_mode = mode_mapping.get(mode, DOTA_GameMode.DOTA_GAMEMODE_CM)
        series_value = series_mapping.get(series_type.lower(), 0)
        
        # Настройки лобби с League ID турнира
        options = {
            'game_name': lobby_name,
            'pass_key': lobby_password,
            'server_region': server_region,
            'game_mode': game_mode,
            'series_type': series_value,  # Серия игр (bo1, bo2, bo3, bo5)
            'allow_spectating': False,
            'allow_cheats': False,
            'dota_tv_delay': 2,
            'fill_with_bots': False,
            'cm_pick': 1,  # Captains Mode: подброс монетки для выбора стороны (право первого выбора)
            'radiant_series_wins': 0,
            'dire_series_wins': 0,
            'leagueid': 18390,  # ID турнира для отображения в настройках лобби
        }
        
        # Создаем practice лобби с турнирными настройками (автоматически закрывается при отключении)
        local_logger.info(f"[{username}] Создание лобби с League ID: 18390...")
        dota.create_practice_lobby(
            password=lobby_password,
            options=options
        )
        
        # Ждем создания лобби (макс 60 сек)
        local_logger.info(f"[{username}] Ожидание создания лобби...")
        
        if lobby_created.wait(timeout=60):
            local_logger.info(f"[{username}] Лобби создано! Применяем настройки...")
            
            # ВАЖНО: Применяем настройки к созданному лобби
            try:
                dota.config_practice_lobby(options=options)
                local_logger.info(f"[{username}] Настройки применены")
                gevent.sleep(0.5)  # Минимальная задержка для применения настроек
            except Exception as e:
                local_logger.warning(f"[{username}] Ошибка применения настроек: {e}")
            
            # ВАЖНО: Заходим в слот наблюдателя (team=4) чтобы загрузиться в игру
            try:
                # Сначала занимаем канал трансляции
                dota.join_practice_lobby_broadcast_channel(channel=1)
                local_logger.info(f"[{username}] Занят слот в канале трансляции")
                gevent.sleep(0.5)  # Минимальная задержка перед присоединением к команде
                
                # Затем присоединяемся к слоту наблюдателя чтобы загрузиться в игру
                dota.join_practice_lobby_team(team=4)
                local_logger.info(f"[{username}] ✅ Присоединились к слоту наблюдателя (team=4)")
            except Exception as e:
                local_logger.warning(f"[{username}] Ошибка входа: {e}")
            
            local_logger.info(f"[{username}] ✅ Лобби полностью настроено!")
            result_queue.put({
                'success': True,
                'lobby_name': lobby_name,
                'password': lobby_password,
                'account': username,
                'server': server,
                'mode': mode,
                'series_type': series_type
            })
        else:
            local_logger.error(f"[{username}] Таймаут создания лобби")
            result_queue.put({'success': False, 'error': 'Lobby creation timeout'})
            # ВАЖНО: Выходим из функции при ошибке, иначе код продолжит выполняться!
            return
        
        # Держим процесс живым 5 минут, автостарт в зависимости от режима
        # Mid Only и 1v1 Solo Mid - оба режима для 1v1 (2 игрока)
        is_1v1 = (mode in ['1v1 Solo Mid', 'Mid Only'])
        required_radiant = 1 if is_1v1 else 5
        required_dire = 1 if is_1v1 else 5
        total_required = required_radiant + required_dire
        
        if is_1v1:
            local_logger.info(f"[{username}] 🔄 Лобби активно, автостарт при 2 игроках (1 vs 1)...")
        else:
            local_logger.info(f"[{username}] 🔄 Лобби активно, автостарт при 10 игроках (5 vs 5)...")
        
        game_started = False
        draft_started = False  # Флаг запуска draft фазы
        players_warned = False  # Флаг предупреждения об игроках
        
        local_logger.info(f"[{username}] 🔄 НАЧИНАЕМ ЦИКЛ ПРОВЕРКИ ИГРОКОВ...")
        
        # Проверяем каждые 3 секунды (700 раз = 35 минут)
        for i in range(700):
            gevent.sleep(3)
            
            # Проверяем команду закрытия
            if shutdown_event.is_set():
                local_logger.info(f"[{username}] 🛑 Получена команда закрытия лобби!")
                break
            
            # Проверяем состояние лобби
            try:
                # Проверяем что лобби еще существует
                lobby_exists = dota.lobby is not None
                
                if not lobby_exists:
                    # Если draft был запущен, а лобби закрылось - значит игра началась
                    if draft_started and not game_started:
                        local_logger.info(f"[{username}] ✅ Draft завершён, игра началась (лобби закрылось для перехода в игру)!")
                        game_started = True
                        # Выходим из цикла проверки игроков, переходим к ожиданию завершения игры
                        break
                    elif not game_started:
                        local_logger.warning(f"[{username}] ⚠️ dota.lobby = None! Лобби закрылось.")
                        result_queue.put({'success': False, 'lobby_closed': True})
                        local_logger.info(f"[{username}] ⏳ Ожидание обработки сообщения о закрытии (15 секунд)...")
                        gevent.sleep(15)
                        break
                    else:
                        # Игра была запущена, лобби закрылось - игра завершена
                        break
                
                # Используем all_members вместо members
                if not hasattr(dota.lobby, 'all_members'):
                    # Если draft запущен, продолжаем ждать
                    if draft_started:
                        continue
                    continue
                
                members_count = len(dota.lobby.all_members)
                
                if members_count == 0:
                    # Если draft запущен, продолжаем ждать
                    if draft_started:
                        continue
                    continue
                
                # Есть игроки! Проверяем их команды
                lobby = dota.lobby
                
                # Подсчитываем игроков в командах
                radiant_players = sum(1 for m in lobby.all_members if m.team == 0)  # 0 = Radiant
                dire_players = sum(1 for m in lobby.all_members if m.team == 1)     # 1 = Dire
                
                # Если draft уже запущен, ждём начала игры
                if draft_started:
                    # Проверяем, началась ли игра (после завершения draft)
                    # Для режимов с draft после завершения выбора игра начинается автоматически
                    # Можно проверить состояние лобби или другие признаки
                    # Пока просто продолжаем ждать - игра начнётся автоматически
                    # Проверяем каждые 10 секунд
                    if i % 3 == 0:  # Каждые ~9 секунд
                        local_logger.info(f"[{username}] ⏳ Ожидание завершения выбора сторон/героев... (проверка {i + 1}/700)")
                    continue
                
                # Проверяем готовность в зависимости от режима
                if radiant_players == required_radiant and dire_players == required_dire:
                    if is_1v1:
                        local_logger.info(f"[{username}] ✅✅✅ 2 ИГРОКА ГОТОВЫ (1 vs 1)! ЗАПУСКАЕМ ИГРУ...")
                    else:
                        local_logger.info(f"[{username}] ✅✅✅ 10 ИГРОКОВ ГОТОВЫ (5 vs 5)! ЗАПУСКАЕМ ИГРУ...")
                    
                    # Проверяем, что бот в канале трансляции
                    local_logger.info(f"[{username}] 📡 Проверяем статус в канале трансляции...")
                    local_logger.info(f"[{username}] 📡 dota.lobby.state = {dota.lobby.state if hasattr(dota.lobby, 'state') else 'N/A'}")
                    
                    # Для 1v1: проверяем, что команды назначены
                    if is_1v1:
                        # Доп.проверка: у обеих сторон должна быть назначена команда (team_id != 0)
                        def teams_assigned(lobby_obj):
                            """Пытаемся определить назначение команд максимально широко.
                            Возвращает True, если у Radiant и Dire есть ненулевые team_id/объекты команды.
                            """
                            try:
                                # 1) Прямые поля ID на лобби
                                candidates = [
                                    (
                                        getattr(lobby_obj, 'team_id_radiant', None) or getattr(lobby_obj, 'radiant_team_id', None),
                                        getattr(lobby_obj, 'team_id_dire', None) or getattr(lobby_obj, 'dire_team_id', None),
                                    )
                                ]
                                # 2) Объекты команд (radiant_team/dire_team) с полями team_id/id
                                r_obj = getattr(lobby_obj, 'radiant_team', None)
                                d_obj = getattr(lobby_obj, 'dire_team', None)
                                if r_obj or d_obj:
                                    r_tid = getattr(r_obj, 'team_id', None) or getattr(r_obj, 'id', None)
                                    d_tid = getattr(d_obj, 'team_id', None) or getattr(d_obj, 'id', None)
                                    candidates.append((r_tid, d_tid))
                                # 3) team_details (официальное место хранения команд в лобби)
                                # team_details - это массив, где индекс 0 = Radiant, индекс 1 = Dire
                                try:
                                    details = getattr(lobby_obj, 'team_details', None)
                                    if details:
                                        details_list = list(details)
                                        # Проверяем оба элемента массива по индексу
                                        if len(details_list) >= 2:
                                            # Radiant (индекс 0)
                                            td_radiant = details_list[0]
                                            radiant_id = getattr(td_radiant, 'team_id', None) or getattr(td_radiant, 'id', None)
                                            radiant_tag = getattr(td_radiant, 'team_tag', None) or getattr(td_radiant, 'tag', None)
                                            radiant_ok = (radiant_id is not None and radiant_id != 0) or (radiant_tag is not None and radiant_tag != '')
                                            
                                            # Dire (индекс 1)
                                            td_dire = details_list[1]
                                            dire_id = getattr(td_dire, 'team_id', None) or getattr(td_dire, 'id', None)
                                            dire_tag = getattr(td_dire, 'team_tag', None) or getattr(td_dire, 'tag', None)
                                            dire_ok = (dire_id is not None and dire_id != 0) or (dire_tag is not None and dire_tag != '')
                                            
                                            if radiant_ok and dire_ok:
                                                return True
                                        # Альтернативная проверка: если team_details содержит элементы с team=0 и team=1
                                        radiant_ok, dire_ok = False, False
                                        for td in details_list:
                                            t_side = getattr(td, 'team', None)
                                            t_id = getattr(td, 'team_id', None) or getattr(td, 'id', None)
                                            t_tag = getattr(td, 'team_tag', None) or getattr(td, 'tag', None)
                                            if t_side == 0 and ((t_id is not None and t_id != 0) or (t_tag is not None and t_tag != '')):
                                                radiant_ok = True
                                            if t_side == 1 and ((t_id is not None and t_id != 0) or (t_tag is not None and t_tag != '')):
                                                dire_ok = True
                                        if radiant_ok and dire_ok:
                                            return True
                                except Exception:
                                    pass

                                # 4) По членам лобби (team/tag/id)
                                has_r, has_d = False, False
                                for mem in getattr(lobby_obj, 'all_members', []) or []:
                                    t = getattr(mem, 'team', None)
                                    # встречаются разные варианты имён атрибутов
                                    tid = (
                                        getattr(mem, 'team_id', 0)
                                        or getattr(mem, 'teamid', 0)
                                        or getattr(mem, 'teamId', 0)
                                    )
                                    tag = getattr(mem, 'team_tag', None) or getattr(mem, 'teamTag', None)
                                    if t == 0 and (tid or tag):
                                        has_r = True
                                    if t == 1 and (tid or tag):
                                        has_d = True
                                if has_r and has_d:
                                    return True
                                # Проверяем кандидатов
                                for r_tid, d_tid in candidates:
                                    if (isinstance(r_tid, int) and r_tid > 0) and (isinstance(d_tid, int) and d_tid > 0):
                                        return True
                                # Если не смогли определить — выводим диагностический лог
                                try:
                                    local_logger.info(
                                        f"[{username}] 🔍 Нет явных team_id. Атрибуты lobby с 'team': "
                                    )
                                    for name in dir(lobby_obj):
                                        if 'team' in name.lower():
                                            val = getattr(lobby_obj, name)
                                            if isinstance(val, (int, str)):
                                                local_logger.info(f"    lobby.{name} = {val}")
                                            else:
                                                local_logger.info(f"    lobby.{name} = {type(val).__name__}")
                                    # Вывести team_details содержимое
                                    details = getattr(lobby_obj, 'team_details', None)
                                    if details:
                                        for idx, td in enumerate(list(details)):
                                            try:
                                                local_logger.info(
                                                    f"    team_details[{idx}]: team={getattr(td,'team',None)} id={getattr(td,'team_id',None) or getattr(td,'id',None)} tag={getattr(td,'team_tag',None) or getattr(td,'tag',None)} name={getattr(td,'team_name',None) or getattr(td,'name',None)}"
                                                )
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                                return False
                            except Exception:
                                return False

                        if not (hasattr(dota, 'lobby') and dota.lobby and hasattr(dota.lobby, 'all_members')):
                            continue
                        r_now = sum(1 for m in dota.lobby.all_members if m.team == 0)
                        d_now = sum(1 for m in dota.lobby.all_members if m.team == 1)
                        if r_now != required_radiant or d_now != required_dire:
                            continue
                        if not teams_assigned(dota.lobby):
                            local_logger.info(f"[{username}] ⚠️ Команды не назначены для обеих сторон — не запускаем.")
                            continue

                    gevent.sleep(2)
                    
                    # Для всех режимов (включая CM/CD, 1v1 Solo Mid, Mid Only) сначала запускаем выбор сторон/героев
                    # Режимы с выбором сторон/героев:
                    modes_with_draft = ['Captains Mode', 'Captains Draft', '1v1 Solo Mid', 'Mid Only']
                    needs_draft = mode in modes_with_draft
                    
                    if needs_draft:
                        local_logger.info(f"[{username}] 🎲 Команды назначены! НАЧИНАЕМ ВЫБОР СТОРОН/ГЕРОЕВ (draft phase)...")
                        local_logger.info(f"[{username}] 📡 Для режима {mode} запускаем выбор сторон/героев...")
                        
                        # Применяем настройки для режимов с выбором (если нужно)
                        if mode in ['Captains Mode', 'Captains Draft']:
                            local_logger.info(f"[{username}] 🪙 Применяем настройки для подброса монетки (cm_pick: 1)...")
                            try:
                                server_mapping = {
                                    'Stockholm': 8,
                                    'Europe West': EServerRegion.Europe,
                                    'Russia': EServerRegion.Europe,
                                    'US East': EServerRegion.USEast,
                                    'US West': EServerRegion.USWest,
                                }
                                mode_mapping = {
                                    'Captains Mode': DOTA_GameMode.DOTA_GAMEMODE_CM,
                                    'All Pick': DOTA_GameMode.DOTA_GAMEMODE_AP,
                                    'Captains Draft': DOTA_GameMode.DOTA_GAMEMODE_CD,
                                    'Mid Only': DOTA_GameMode.DOTA_GAMEMODE_MO,
                                    '1v1 Solo Mid': DOTA_GameMode.DOTA_GAMEMODE_1V1MID,
                                    'Random Draft': DOTA_GameMode.DOTA_GAMEMODE_RD,
                                    'Single Draft': DOTA_GameMode.DOTA_GAMEMODE_SD,
                                }
                                series_mapping = {
                                    'bo1': 0,
                                    'bo2': 1,
                                    'bo3': 2,
                                    'bo5': 3,
                                }
                                server_region = server_mapping.get(server, EServerRegion.Europe)
                                game_mode = mode_mapping.get(mode, DOTA_GameMode.DOTA_GAMEMODE_CM)
                                series_value = series_mapping.get(series_type.lower(), 0)
                                
                                cm_options = {
                                    'game_name': lobby_name,
                                    'pass_key': lobby_password,
                                    'server_region': server_region,
                                    'game_mode': game_mode,
                                    'series_type': series_value,
                                    'allow_spectating': False,
                                    'allow_cheats': False,
                                    'dota_tv_delay': 2,
                                    'fill_with_bots': False,
                                    'cm_pick': 1,  # КРИТИЧНО: подброс монетки для выбора стороны
                                    'radiant_series_wins': 0,
                                    'dire_series_wins': 0,
                                    'leagueid': 18390,
                                }
                                dota.config_practice_lobby(options=cm_options)
                                local_logger.info(f"[{username}] ✅ Настройки cm_pick: 1 применены")
                                gevent.sleep(1)  # Даём время на применение настроек
                            except Exception as cm_error:
                                local_logger.warning(f"[{username}] ⚠️ Ошибка применения cm_pick: {cm_error}")
                        
                        # Запускаем выбор сторон/героев (draft фазу)
                        # Для режимов с выбором launch_practice_lobby() запускает draft, а не игру
                        dota.launch_practice_lobby()
                        gevent.sleep(5)  # Даём время на начало draft фазы
                        
                        draft_started = True  # Помечаем, что draft запущен
                        local_logger.info(f"[{username}] ✅ Выбор сторон/героев запущен! Игроки выбирают...")
                        local_logger.info(f"[{username}] ⏳ Ожидание завершения выбора сторон/героев...")
                        local_logger.info(f"[{username}] 💡 После завершения выбора игра начнётся автоматически!")
                        
                        # После launch_practice_lobby() начинается выбор сторон/героев,
                        # а после завершения выбора игра начинается автоматически
                        # НЕ помечаем игру как запущенную - продолжаем цикл проверки
                        # Бот будет отслеживать начало игры в основном цикле
                        # После завершения draft игра начнётся автоматически, и мы это зафиксируем
                        # Продолжаем цикл проверки, чтобы отслеживать начало игры
                        continue  # Продолжаем цикл проверки, не помечая игру как запущенную
                    else:
                        # Для режимов без выбора (All Pick, Random Draft и т.д.) запускаем игру сразу
                        local_logger.info(f"[{username}] 🚀 ЗАПУСКАЕМ ИГРУ...")
                        dota.launch_practice_lobby()
                        gevent.sleep(5)  # Даём время на запуск
                        
                        local_logger.info(f"[{username}] 🎮🎮🎮 ИГРА ЗАПУЩЕНА! Бот загружается как наблюдатель!")
                        
                        game_started = True
                        break
                        
            except Exception as check_error:
                local_logger.error(f"[{username}] ❌ ОШИБКА при проверке игроков: {check_error}", exc_info=True)
        
        # ВАЖНО: Явно удаляем лобби ПЕРЕД отключением (но только если игра не запущена)
        if not game_started:
            local_logger.info(f"[{username}] Удаление лобби...")
            try:
                dota.destroy_lobby()
                gevent.sleep(3)  # Даём время серверам Valve обработать destroy
                local_logger.info(f"[{username}] ✅ Лобби удалено")
            except Exception as destroy_error:
                local_logger.warning(f"[{username}] Ошибка при удалении лобби: {destroy_error}")
            
            # Отключаемся от Steam
            try:
                dota.leave_practice_lobby()
                gevent.sleep(1)
                steam.disconnect()
                local_logger.info(f"[{username}] Отключились от Steam")
            except Exception as disconnect_error:
                local_logger.warning(f"[{username}] Ошибка при отключении: {disconnect_error}")
        else:
            # Игра запущена - остаемся подключенными до получения команды shutdown или закрытия лобби
            local_logger.info(f"[{username}] 🎮 Игра запущена! Бот остается в Steam для поддержки лобби...")
            local_logger.info(f"[{username}] ⏳ Ожидание завершения игры или команды закрытия...")
            
            # Держим процесс живым пока идет игра или не получен сигнал shutdown
            while not shutdown_event.is_set():
                gevent.sleep(5)  # Проверяем каждые 5 секунд
                
                # Проверяем, существует ли ещё лобби (игра может закончиться)
                try:
                    if dota.lobby is None:
                        local_logger.info(f"[{username}] 🏁 Лобби закрылось (игра завершена)!")
                        result_queue.put({'success': False, 'lobby_closed': True})
                        gevent.sleep(15)  # Даем время главному процессу
                        break
                        
                except Exception as check_error:
                    local_logger.warning(f"[{username}] ⚠️ Ошибка проверки лобби: {check_error}")
                    local_logger.info(f"[{username}] 🏁 Ошибка проверки - вероятно лобби закрылось!")
                    result_queue.put({'success': False, 'lobby_closed': True})
                    gevent.sleep(15)
                    break
            
            # Получена команда закрытия или игра завершена
            local_logger.info(f"[{username}] 🛑 Завершение работы...")
            try:
                dota.destroy_lobby()
                gevent.sleep(3)
                local_logger.info(f"[{username}] ✅ Лобби удалено после игры")
            except Exception as destroy_error:
                local_logger.warning(f"[{username}] Ошибка при удалении лобби после игры: {destroy_error}")
            
            try:
                dota.leave_practice_lobby()
                gevent.sleep(1)
                steam.disconnect()
                local_logger.info(f"[{username}] Отключились от Steam после игры")
            except Exception as disconnect_error:
                local_logger.warning(f"[{username}] Ошибка при отключении после игры: {disconnect_error}")
        
    except KeyboardInterrupt:
        local_logger.info(f"[{username}] 🛑 Получен сигнал прерывания (Ctrl+C)!")
        # КРИТИЧНО: Удаляем лобби перед выходом!
        try:
            local_logger.info(f"[{username}] 🗑️ Удаляем лобби перед выходом...")
            dota.leave_practice_lobby()
            gevent.sleep(1)
            dota.destroy_lobby()
            gevent.sleep(2)
            local_logger.info(f"[{username}] ✅ Лобби удалено!")
        except Exception as cleanup_error:
            local_logger.error(f"[{username}] ❌ Ошибка при удалении лобби: {cleanup_error}")
        
        try:
            steam.disconnect()
            local_logger.info(f"[{username}] 👋 Отключились от Steam")
        except:
            pass
            
    except Exception as e:
        local_logger.error(f"[{username}] Ошибка: {e}", exc_info=True)
        result_queue.put({'success': False, 'error': str(e)})


class SteamAccount:
    """Информация об аккаунте"""
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.is_busy = False
        
    def to_dict(self):
        return {
            'username': self.username,
            'password': self.password
        }


class LobbyInfo:
    """Информация о лобби"""
    def __init__(self, lobby_name: str, password: str, account: str):
        self.lobby_name = lobby_name  # "wb cup 1", "wb cup 2"
        self.password = password
        self.account = account
        self.created_at = datetime.now()
        self.players_count = 0
        self.status = "active"


class RealDota2BotV2:
    """Улучшенный бот"""
    
    def __init__(self):
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.admin_ids = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
        self.notification_chat_id = os.getenv('NOTIFICATION_CHAT_ID')
        self.notification_thread_id = int(os.getenv('NOTIFICATION_THREAD_ID', '0')) if os.getenv('NOTIFICATION_THREAD_ID') else None
        
        self.telegram_app = None
        
        # Хранилище
        self.steam_accounts: List[SteamAccount] = []
        self.active_lobbies: Dict[str, LobbyInfo] = {}  # "wb cup 1" -> LobbyInfo
        self.active_processes: Dict[str, Process] = {}  # username -> Process
        self.shutdown_events: Dict[str, multiprocessing.Event] = {}  # username -> Event
        self.result_queues: Dict[str, Queue] = {}  # username -> Queue для мониторинга закрытия лобби
        
        # Настройки
        self.lobby_base_name = "wb cup"  # Базовое название
        self.server_region = "Stockholm"
        self.game_mode = "Captains Mode"
        
        # Счетчик лобби (ВАЖНО: НЕ сохраняем между перезапусками!)
        self.lobby_counter = 1
        
        # Расписание
        self.schedule_config = {}
        self.scheduler = None
        
        # Загрузка
        self.load_accounts()
        self.load_settings()
        self.load_schedule()
        
        # ВАЖНО: Очищаем все аккаунты при старте (новая сессия = новые лобби)
        for account in self.steam_accounts:
            account.is_busy = False
            account.current_lobby = None
            account.bot_instance = None
        
        logger.info("🔄 Все аккаунты освобождены для новой сессии")
        
        # ВАЖНО: Убиваем все старые процессы Python/Steam
        self.kill_old_processes()
        
    def kill_old_processes(self):
        """Убиваем ВСЕ старые процессы Steam/Dota АГРЕССИВНО"""
        try:
            import subprocess
            import time
            
            logger.info("🔪 Очистка ВСЕХ старых процессов Steam/Dota...")
            
            # Убиваем все процессы Steam и Dota с флагом -9 (принудительно)
            commands = [
                ['pkill', '-9', '-f', 'steam'],
                ['pkill', '-9', '-f', 'dota'],
                ['pkill', '-9', '-f', 'SteamClient'],
                ['pkill', '-9', '-f', 'steam_worker'],
            ]
            
            for cmd in commands:
                try:
                    subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=2)
                except:
                    pass
            
            # Даём время на завершение
            time.sleep(2)
            
            logger.info("✅ Все старые процессы убиты!")
        except Exception as e:
            logger.warning(f"Ошибка очистки процессов: {e}")
        
    def load_accounts(self):
        try:
            if os.path.exists('steam_accounts.json'):
                with open('steam_accounts.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for acc_data in data:
                        account = SteamAccount(acc_data['username'], acc_data['password'])
                        self.steam_accounts.append(account)
                logger.info(f"Загружено {len(self.steam_accounts)} аккаунтов")
        except Exception as e:
            logger.error(f"Ошибка загрузки аккаунтов: {e}")
            
    def save_accounts(self):
        try:
            data = [{'username': acc.username, 'password': acc.password} for acc in self.steam_accounts]
            with open('steam_accounts.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")
    
    def load_settings(self):
        try:
            if os.path.exists('lobby_settings.json'):
                with open('lobby_settings.json', 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    self.lobby_base_name = settings.get('lobby_base_name', self.lobby_base_name)
                    self.server_region = settings.get('server_region', self.server_region)
                    self.game_mode = settings.get('game_mode', self.game_mode)
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек: {e}")
    
    def save_settings(self):
        try:
            settings = {
                'lobby_base_name': self.lobby_base_name,
                'server_region': self.server_region,
                'game_mode': self.game_mode
            }
            with open('lobby_settings.json', 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")
    
    def load_schedule(self):
        """Загрузка расписания"""
        try:
            if os.path.exists('schedule_config.json'):
                with open('schedule_config.json', 'r', encoding='utf-8') as f:
                    self.schedule_config = json.load(f)
                logger.info(f"📅 Загружено расписаний: {len(self.schedule_config.get('schedules', []))}")
            else:
                # Создаём пустое расписание
                self.schedule_config = {
                    "enabled": False,
                    "timezone": "Europe/Moscow",
                    "schedules": []
                }
                self.save_schedule()
        except Exception as e:
            logger.error(f"Ошибка загрузки расписания: {e}")
            self.schedule_config = {"enabled": False, "timezone": "Europe/Moscow", "schedules": []}
    
    def save_schedule(self):
        """Сохранение расписания"""
        try:
            with open('schedule_config.json', 'w', encoding='utf-8') as f:
                json.dump(self.schedule_config, f, ensure_ascii=False, indent=2)
            logger.info("💾 Расписание сохранено")
            
            # Перезапускаем планировщик если бот уже запущен
            if hasattr(self, 'telegram_app') and self.telegram_app is not None:
                self.setup_scheduler()
        except Exception as e:
            logger.error(f"Ошибка сохранения расписания: {e}")
    
    def generate_password(self, length=8) -> str:
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(length))
    
    def generate_start_code(self, length=6) -> str:
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    
    def get_available_accounts(self) -> List[SteamAccount]:
        return [acc for acc in self.steam_accounts if not acc.is_busy]
    
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
    
    def get_next_lobby_name(self) -> str:
        """Генерация следующего названия лобби: wb cup 1, wb cup 2..."""
        name = f"{self.lobby_base_name} {self.lobby_counter}"
        self.lobby_counter += 1
        return name
    
    def get_main_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("🎮 Создать лобби", callback_data="create_lobby")],
            [InlineKeyboardButton("📋 Список лобби", callback_data="list_lobbies")],
            [InlineKeyboardButton("🤖 Управление ботами", callback_data="manage_bots")],
            [InlineKeyboardButton("📅 Расписание", callback_data="schedule")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
             InlineKeyboardButton("📊 Статус", callback_data="status")],
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_welcome_text(self):
        return f"""
🎮 <b>Dota 2 Real Lobby Bot v2</b>

<b>📊 Статистика:</b>
🤖 Ботов: {len(self.steam_accounts)}
💚 Свободных: {len(self.get_available_accounts())}
🎯 Активных лобби: {len(self.active_lobbies)}

<b>⚙️ Настройки:</b>
📝 Базовое название: {self.lobby_base_name}
🌍 Сервер: {self.server_region}
🎮 Режим: {self.game_mode}
        """
    
    # ==================== КОМАНДЫ ====================
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if hasattr(update, 'effective_user') else update.from_user.id
        
        if not self.is_admin(user_id):
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text("❌ Нет доступа")
            return
        
        text = self.get_welcome_text()
        keyboard = self.get_main_keyboard()
        
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)
        elif hasattr(update, 'callback_query') and update.callback_query:
            try:
                await update.callback_query.edit_message_text(text, parse_mode='HTML', reply_markup=keyboard)
            except:
                await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if not self.is_admin(query.from_user.id):
            await query.edit_message_text("❌ Нет доступа")
            return
            
        data = query.data
        
        try:
            if data == "create_lobby":
                return await self.handle_create_lobby_request(update, context)
            elif data == "list_lobbies":
                await self.handle_list_lobbies(query)
            elif data == "manage_bots":
                await self.handle_manage_bots(query)
            elif data == "add_bot":
                return await self.handle_add_bot_request(update, context)
            elif data.startswith("delete_bot_"):
                username = data.replace("delete_bot_", "")
                await self.handle_delete_bot_confirm(query, username)
            elif data.startswith("confirm_delete_"):
                username = data.replace("confirm_delete_", "")
                await self.handle_delete_bot(query, username)
            elif data.startswith("edit_bot_"):
                return await self.handle_edit_bot_request(update, context)
            elif data == "select_bots":
                return await self.handle_select_bots_menu(update, context)
            elif data == "schedule":
                await self.handle_schedule(query)
            elif data == "match_view_active":
                await self.handle_view_active_matches(query)
            elif data.startswith("schedule_"):
                await self.handle_schedule_action(query, data)
            elif data.startswith("match_"):
                return await self.handle_match_action(update, context)
            elif data == "settings":
                await self.handle_settings(query)
            elif data == "status":
                await self.handle_status(query)
            elif data == "back_main":
                await self.handle_back_to_main(query)
            elif data.startswith("close_lobby_"):
                lobby_name = data.replace("close_lobby_", "")
                await self.handle_close_lobby(query, lobby_name)
            elif data == "destroy_all_lobbies":
                await self.handle_destroy_all_lobbies(query)
            elif data.startswith("cancel_creation_"):
                username = data.replace("cancel_creation_", "")
                await self.handle_cancel_creation(query, username)
        except Exception as e:
            logger.error(f"Ошибка обработки {data}: {e}", exc_info=True)
            try:
                await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                ]]))
            except:
                pass
    
    # ==================== УПРАВЛЕНИЕ БОТАМИ ====================
    
    async def handle_manage_bots(self, query):
        """Меню управления ботами"""
        if not self.steam_accounts:
            await query.edit_message_text(
                "🤖 <b>Нет ботов</b>\n\nДобавьте аккаунты",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить", callback_data="add_bot"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                ]])
            )
            return
        
        message = "<b>🤖 Управление ботами</b>\n\n"
        keyboard = []
        
        for idx, acc in enumerate(self.steam_accounts, 1):
            status = "🔴 Занят" if acc.is_busy else "🟢 Свободен"
            message += f"{idx}. <code>{acc.username}</code> - {status}\n"
            if acc.current_lobby:
                message += f"   └ Лобби: {acc.current_lobby}\n"
            
            keyboard.append([
                InlineKeyboardButton(f"✏️ Изменить {idx}", callback_data=f"edit_bot_{acc.username}"),
                InlineKeyboardButton(f"🗑️ Удалить {idx}", callback_data=f"delete_bot_{acc.username}")
            ])
        
        message += f"\n<b>Всего:</b> {len(self.steam_accounts)}\n"
        message += f"<b>Свободных:</b> {len(self.get_available_accounts())}"
        
        keyboard.append([
            InlineKeyboardButton("➕ Добавить бота", callback_data="add_bot")
        ])
        
        # Кнопка "Распустить все лобби" (если есть активные)
        if self.active_lobbies:
            keyboard.append([
                InlineKeyboardButton("🔥 Распустить все лобби", callback_data="destroy_all_lobbies")
            ])
        
        keyboard.append([
            InlineKeyboardButton("◀️ Назад", callback_data="back_main")
        ])
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def handle_delete_bot_confirm(self, query, username: str):
        """Подтверждение удаления"""
        account = next((acc for acc in self.steam_accounts if acc.username == username), None)
        
        if not account:
            await query.answer("❌ Бот не найден", show_alert=True)
            return
        
        if account.is_busy:
            await query.edit_message_text(
                f"❌ <b>Нельзя удалить занятый бот!</b>\n\n"
                f"Бот <code>{username}</code> сейчас создает лобби.\n"
                f"Сначала закройте лобби.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data="manage_bots")
                ]])
            )
            return
        
        await query.edit_message_text(
            f"🗑️ <b>Удалить бота?</b>\n\n"
            f"Бот: <code>{username}</code>\n\n"
            f"⚠️ Это действие нельзя отменить!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{username}"),
                InlineKeyboardButton("❌ Отмена", callback_data="manage_bots")
            ]])
        )
    
    async def handle_delete_bot(self, query, username: str):
        """Удаление бота"""
        account = next((acc for acc in self.steam_accounts if acc.username == username), None)
        
        if account:
            self.steam_accounts.remove(account)
            self.save_accounts()
            
            await query.answer(f"✅ Бот {username} удален!", show_alert=True)
            await self.handle_manage_bots(query)
        else:
            await query.answer("❌ Бот не найден", show_alert=True)
    
    async def handle_edit_bot_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрос на редактирование бота"""
        query = update.callback_query
        username = query.data.replace("edit_bot_", "")
        account = next((acc for acc in self.steam_accounts if acc.username == username), None)
        
        if not account:
            await query.answer("❌ Бот не найден", show_alert=True)
            return ConversationHandler.END
        
        if account.is_busy:
            await query.edit_message_text(
                f"❌ <b>Нельзя изменить занятый бот!</b>\n\n"
                f"Бот <code>{username}</code> сейчас создает лобби.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data="manage_bots")
                ]])
            )
            return ConversationHandler.END
        
        context.user_data['editing_bot'] = username
        
        await query.edit_message_text(
            f"<b>✏️ Редактирование бота</b>\n\n"
            f"Текущий логин: <code>{username}</code>\n\n"
            f"Отправьте новые данные в формате:\n"
            f"<code>новый_логин новый_пароль</code>\n\n"
            f"Или отправьте /cancel для отмены",
            parse_mode='HTML'
        )
        
        return WAITING_EDIT_BOT_DATA
    
    async def handle_edit_bot_data_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка новых данных бота"""
        try:
            old_username = context.user_data.get('editing_bot')
            parts = update.message.text.strip().split()
            
            if len(parts) != 2:
                await update.message.reply_text(
                    "❌ Неверный формат!\n\nИспользуйте: <code>логин пароль</code>",
                    parse_mode='HTML'
                )
                return WAITING_EDIT_BOT_DATA
            
            new_username, new_password = parts
            
            account = next((acc for acc in self.steam_accounts if acc.username == old_username), None)
            
            if not account:
                await update.message.reply_text("❌ Бот не найден", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            
            # Проверка дубликата
            if new_username != old_username and any(acc.username == new_username for acc in self.steam_accounts):
                await update.message.reply_text(
                    f"❌ Бот <code>{new_username}</code> уже существует!",
                    parse_mode='HTML'
                )
                return WAITING_EDIT_BOT_DATA
            
            # Обновляем
            account.username = new_username
            account.password = new_password
            self.save_accounts()
            
            await update.message.reply_text(
                f"✅ <b>Бот обновлен!</b>\n\n"
                f"Старый логин: <code>{old_username}</code>\n"
                f"Новый логин: <code>{new_username}</code>",
                parse_mode='HTML',
                reply_markup=self.get_main_keyboard()
            )
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Ошибка редактирования: {e}")
            await update.message.reply_text("❌ Ошибка")
            return WAITING_EDIT_BOT_DATA
    
    async def handle_add_bot_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.edit_message_text(
            "<b>➕ Добавление аккаунта</b>\n\n"
            "Формат: <code>логин пароль</code>\n\n"
            "Пример: <code>mylogin123 mypass456</code>",
            parse_mode='HTML'
        )
        return WAITING_ACCOUNT_DATA
    
    async def handle_account_data_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            parts = update.message.text.strip().split()
            
            if len(parts) != 2:
                await update.message.reply_text("❌ Формат: <code>логин пароль</code>", parse_mode='HTML')
                return WAITING_ACCOUNT_DATA
            
            username, password = parts
            
            if any(acc.username == username for acc in self.steam_accounts):
                await update.message.reply_text(f"❌ Бот <code>{username}</code> уже добавлен!", parse_mode='HTML')
                return WAITING_ACCOUNT_DATA
            
            account = SteamAccount(username, password)
            self.steam_accounts.append(account)
            self.save_accounts()
            
            await update.message.reply_text(
                f"✅ <b>Бот добавлен!</b>\n\n"
                f"Логин: <code>{username}</code>\n"
                f"Всего: {len(self.steam_accounts)}",
                parse_mode='HTML',
                reply_markup=self.get_main_keyboard()
            )
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Ошибка добавления: {e}")
            await update.message.reply_text("❌ Ошибка")
            return WAITING_ACCOUNT_DATA
    
    # ==================== СОЗДАНИЕ ЛОББИ С ВЫБОРОМ БОТОВ ====================
    
    async def handle_create_lobby_request(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        available = len(self.get_available_accounts())
        
        if available == 0:
            await query.edit_message_text(
                "❌ <b>Нет доступных ботов!</b>\n\nДобавьте аккаунты",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить", callback_data="add_bot"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                ]])
            )
            return ConversationHandler.END
        
        # Инициализируем выбранных ботов
        context.user_data['selected_bots'] = []
        
        await query.edit_message_text(
            f"<b>🎮 Создание лобби</b>\n\n"
            f"Доступно ботов: <b>{available}</b>\n\n"
            f"⚠️ Бот зайдет в Steam и создаст лобби в Dota 2!\n\n"
            f"Выберите ботов для создания лобби:",
            parse_mode='HTML'
        )
        
        return await self.handle_select_bots_menu(update, context)
    
    async def handle_select_bots_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора ботов"""
        query = update.callback_query if hasattr(update, 'callback_query') else None
        
        selected = context.user_data.get('selected_bots', [])
        available = self.get_available_accounts()
        
        message = "<b>🎮 Выбор ботов для лобби</b>\n\n"
        message += f"Выбрано: <b>{len(selected)}</b>\n\n"
        
        keyboard = []
        for acc in available:
            is_selected = acc.username in selected
            emoji = "✅" if is_selected else "⬜"
            keyboard.append([
                InlineKeyboardButton(
                    f"{emoji} {acc.username}",
                    callback_data=f"toggle_bot_{acc.username}"
                )
            ])
        
        if selected:
            keyboard.append([
                InlineKeyboardButton(f"🎮 Создать {len(selected)} лобби", callback_data="confirm_bot_selection")
            ])
        
        keyboard.append([
            InlineKeyboardButton("❌ Отмена", callback_data="back_main")
        ])
        
        if query:
            try:
                await query.edit_message_text(
                    message,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                await query.message.reply_text(
                    message,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        
        return WAITING_SELECT_BOTS
    
    async def handle_toggle_bot_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Переключение выбора бота"""
        query = update.callback_query
        
        # Извлекаем username из callback_data
        username = query.data.replace("toggle_bot_", "")
        
        selected = context.user_data.get('selected_bots', [])
        
        if username in selected:
            selected.remove(username)
            await query.answer(f"❌ {username} убран", show_alert=False)
        else:
            selected.append(username)
            await query.answer(f"✅ {username} выбран", show_alert=False)
        
        context.user_data['selected_bots'] = selected
        
        # Обновляем меню
        await self.handle_select_bots_menu(update, context)
    
    async def handle_confirm_bot_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подтверждение выбора ботов и переход к выбору режима игры"""
        query = update.callback_query
        selected = context.user_data.get('selected_bots', [])
        
        if not selected:
            await query.answer("❌ Выберите хотя бы 1 бота", show_alert=True)
            return WAITING_SELECT_BOTS
        
        count = len(selected)
        
        # Переход к выбору режима игры
        keyboard = [
            [InlineKeyboardButton("⚔️ Captains Mode", callback_data="mode_Captains Mode")],
            [InlineKeyboardButton("🎲 All Pick", callback_data="mode_All Pick")],
            [InlineKeyboardButton("📋 Captains Draft", callback_data="mode_Captains Draft")],
            [InlineKeyboardButton("🎯 Mid Only", callback_data="mode_Mid Only")],
            [InlineKeyboardButton("🥊 1v1 Solo Mid", callback_data="mode_1v1 Solo Mid")],
            [InlineKeyboardButton("◀️ Назад", callback_data="select_bots")]
        ]
        
        await query.edit_message_text(
            f"<b>🎮 Выбор режима игры</b>\n\n"
            f"Выбрано ботов: <b>{count}</b>\n\n"
            f"Выберите режим игры:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return WAITING_GAME_MODE
    
    async def handle_game_mode_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора режима игры и переход к выбору серии"""
        query = update.callback_query
        
        # Извлекаем режим из callback_data
        game_mode = query.data.replace("mode_", "")
        context.user_data['game_mode'] = game_mode
        
        count = len(context.user_data.get('selected_bots', []))
        
        # Переход к выбору серии игр
        keyboard = [
            [InlineKeyboardButton("1️⃣ Одна игра (BO1)", callback_data="series_bo1")],
            [InlineKeyboardButton("2️⃣ Две игры (BO2)", callback_data="series_bo2")],
            [InlineKeyboardButton("3️⃣ До 2 побед (BO3)", callback_data="series_bo3")],
            [InlineKeyboardButton("5️⃣ До 3 побед (BO5)", callback_data="series_bo5")],
            [InlineKeyboardButton("◀️ Назад", callback_data="confirm_bot_selection")]
        ]
        
        await query.edit_message_text(
            f"<b>🎯 Выбор серии игр</b>\n\n"
            f"Ботов: <b>{count}</b>\n"
            f"Режим: <b>{game_mode}</b>\n\n"
            f"Выберите тип серии:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return WAITING_SERIES_TYPE
    
    async def handle_series_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка выбора серии и создание лобби"""
        query = update.callback_query
        
        # Извлекаем серию из callback_data
        series_type = query.data.replace("series_", "")
        context.user_data['series_type'] = series_type
        
        selected = context.user_data.get('selected_bots', [])
        game_mode = context.user_data.get('game_mode')
        count = len(selected)
        
        status_msg = await query.edit_message_text(
            f"⏳ <b>Создаю {count} лобби...</b>\n\n"
            f"🎮 Режим: {game_mode}\n"
            f"🎯 Серия: {series_type.upper()}\n\n"
            f"🔄 Подключение к Steam и запуск Dota 2...\n"
            f"<i>Это может занять 1-2 минуты</i>",
            parse_mode='HTML'
        )
        
        # Получаем выбранные аккаунты
        selected_accounts = [acc for acc in self.steam_accounts if acc.username in selected]
        
        created_lobbies = await self.create_multiple_real_lobbies_from_accounts(
            selected_accounts,
            status_msg,
            context,
            game_mode=game_mode,
            series_type=series_type
        )
        
        if not created_lobbies:
            await status_msg.edit_text(
                "❌ <b>Не удалось создать лобби</b>\n\n"
                "Проверьте логи для деталей",
                parse_mode='HTML',
                reply_markup=self.get_main_keyboard()
            )
            return ConversationHandler.END
        
        # Результат
        message = f"✅ <b>Создано {len(created_lobbies)} лобби!</b>\n\n"
        message += f"🎮 Режим: <b>{game_mode}</b>\n"
        message += f"🎯 Серия: <b>{series_type.upper()}</b>\n\n"
        
        for idx, lobby in enumerate(created_lobbies, 1):
            message += f"<b>{idx}. {lobby.lobby_name}</b>\n"
            message += f"🔒 Пароль: <code>{lobby.password}</code>\n"
            message += f"🤖 Бот: {lobby.account}\n\n"
        
        message += "<b>🎮 Лобби созданы в игре!</b>\n"
        message += "<i>Игроки ищут по названию: wb cup 1, wb cup 2...</i>"
        
        # Для уведомления в группу - делаем весь текст жирным
        group_message = f"<b>✅ Создано {len(created_lobbies)} лобби!\n\n"
        group_message += f"🎮 Режим: {game_mode}\n"
        group_message += f"🎯 Серия: {series_type.upper()}\n\n"
        
        for idx, lobby in enumerate(created_lobbies, 1):
            group_message += f"{idx}. {lobby.lobby_name}\n"
            group_message += f"🔒 Пароль: <code>{lobby.password}</code>\n"
            group_message += f"🤖 Бот: {lobby.account}\n\n"
        
        group_message += "🎮 Лобби созданы в игре!\n"
        group_message += "Игроки ищут по названию: wb cup 1, wb cup 2...</b>"
        
        await status_msg.edit_text(
            message,
            parse_mode='HTML',
            reply_markup=self.get_main_keyboard()
        )
        
        # Уведомление в топик группы
        if self.notification_chat_id:
            try:
                send_kwargs = {
                    'chat_id': self.notification_chat_id,
                    'text': group_message,
                    'parse_mode': 'HTML'
                }
                # Если указан ID топика - отправляем в топик
                if self.notification_thread_id:
                    send_kwargs['message_thread_id'] = self.notification_thread_id
                
                await context.bot.send_message(**send_kwargs)
                logger.info(f"✅ Уведомление отправлено в {'топик ' + str(self.notification_thread_id) if self.notification_thread_id else 'чат'}")
            except Exception as e:
                logger.error(f"Ошибка уведомления: {e}")
        
        return ConversationHandler.END
    
    async def create_multiple_real_lobbies_from_accounts(
        self,
        accounts: List[SteamAccount],
        status_msg,
        context,
        game_mode: str = None,
        series_type: str = None
    ) -> List[LobbyInfo]:
        """Создание лобби из выбранных аккаунтов"""
        created = []
        total = len(accounts)
        
        for idx, account in enumerate(accounts, 1):
            try:
                await status_msg.edit_text(
                    f"⏳ <b>Создание лобби {idx}/{total}</b>\n\n"
                    f"🤖 Аккаунт: {account.username}\n"
                    f"🔄 Подключение к Steam...",
                    parse_mode='HTML'
                )
                
                account.is_busy = True
                lobby_info = await self.create_single_real_lobby(
                    account, 
                    status_msg,
                    game_mode=game_mode,
                    series_type=series_type
                )
                
                if lobby_info:
                    created.append(lobby_info)
                    logger.info(f"✅ Лобби {idx} создано: {lobby_info.lobby_name}")
                else:
                    logger.error(f"❌ Не удалось создать лобби {idx}")
                    account.is_busy = False
                    
            except Exception as e:
                logger.error(f"Ошибка создания лобби {idx}: {e}", exc_info=True)
                account.is_busy = False
        
        return created
    
    async def create_single_real_lobby(self, account: SteamAccount, status_msg, 
                                       game_mode: str = None, series_type: str = None, 
                                       lobby_name: str = None) -> Optional[LobbyInfo]:
        """Создание лобби через Steam и Dota 2 в отдельном процессе"""
        process = None
        result_queue = None
        shutdown_event = None
        
        try:
            # Генерируем данные
            if not lobby_name:
                lobby_name = self.get_next_lobby_name()
            if not game_mode:
                game_mode = self.game_mode
            if not series_type:
                series_type = "bo1"  # По умолчанию одна игра
            
            password = self.generate_password()
            
            # Обновляем статус с кнопкой отмены
            cancel_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отменить создание", callback_data=f"cancel_creation_{account.username}")
            ]])
            await status_msg.edit_text(
                f"⏳ <b>Создание реального лобби</b>\n\n"
                f"🤖 Аккаунт: {account.username}\n"
                f"🏷️ Название: {lobby_name}\n"
                f"🔐 Пароль: {password}\n\n"
                f"⏱️ Запуск Steam...",
                parse_mode='HTML',
                reply_markup=cancel_keyboard
            )
            
            # Создаем очередь для результата и event для shutdown
            result_queue = multiprocessing.Queue()
            shutdown_event = multiprocessing.Event()
            
            # Запускаем Steam в отдельном процессе
            process = Process(
                target=steam_worker_process,
                args=(
                    account.username,
                    account.password,
                    lobby_name,
                    password,
                    self.server_region,
                    game_mode,  # Режим игры
                    series_type,  # Серия игр
                    result_queue,
                    shutdown_event,
                )
            )
            process.start()
            
            # Сохраняем shutdown_event для возможности закрытия лобби
            self.shutdown_events[account.username] = shutdown_event
            
            # Ждем результата (с таймаутом)
            max_wait_time = 180  # 3 минуты (увеличено для медленных соединений)
            start_time = time.time()
            result = None
            
            while time.time() - start_time < max_wait_time:
                await asyncio.sleep(2)
                
                # Обновляем статус каждые 10 секунд
                elapsed = int(time.time() - start_time)
                if elapsed % 10 == 0:
                    await status_msg.edit_text(
                        f"⏳ <b>Создание реального лобби</b>\n\n"
                        f"🤖 Аккаунт: {account.username}\n"
                        f"🏷️ Название: {lobby_name}\n"
                        f"🔐 Пароль: {password}\n\n"
                        f"⏱️ Прошло {elapsed} сек...",
                        parse_mode='HTML',
                        reply_markup=cancel_keyboard
                    )
                
                # Проверяем очередь
                if not result_queue.empty():
                    result = result_queue.get()
                    break
            
            # Анализируем результат
            if result and result.get('success'):
                logger.info(f"✅ Лобби создано: {lobby_name}")
                
                # Создаем объект лобби
                lobby_info = LobbyInfo(
                    lobby_name=lobby_name,
                    password=password,
                    account=account.username,
                )
                
                # КРИТИЧЕСКИ ВАЖНО: Сохраняем процесс и очередь ПЕРВЫМИ для мониторинга
                self.active_processes[account.username] = process
                self.result_queues[account.username] = result_queue
                
                # Теперь обновляем статусы (после сохранения очереди!)
                self.active_lobbies[lobby_name] = lobby_info
                account.is_busy = True
                account.current_lobby = lobby_name
                
                return lobby_info
            else:
                error_msg = result.get('error', 'Unknown error') if result else 'Timeout'
                logger.error(f"❌ Не удалось создать лобби: {error_msg}")
                
                # Освобождаем аккаунт
                account.is_busy = False
                
                # Отправляем shutdown signal и останавливаем процесс
                if process and process.is_alive():
                    if shutdown_event:
                        logger.info(f"Отправляем shutdown signal для {account.username}...")
                        shutdown_event.set()
                        process.join(timeout=10)  # Даём 10 сек на graceful shutdown
                    
                    if process.is_alive():
                        logger.warning(f"Процесс {account.username} не завершился, принудительное завершение...")
                        process.terminate()
                        process.join(timeout=5)
                    
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=2)
                
                # Очистка
                if account.username in self.active_processes:
                    del self.active_processes[account.username]
                if account.username in self.shutdown_events:
                    del self.shutdown_events[account.username]
                
                return None
            
        except Exception as e:
            logger.error(f"Ошибка создания РЕАЛЬНОГО лобби: {e}", exc_info=True)
            
            # Освобождаем аккаунт
            account.is_busy = False
            
            # Останавливаем процесс
            if process and process.is_alive():
                try:
                    if shutdown_event:
                        shutdown_event.set()
                        process.join(timeout=10)
                    
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                    
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=2)
                except:
                    pass
            
            # Очистка
            if account.username in self.active_processes:
                del self.active_processes[account.username]
            if account.username in self.shutdown_events:
                del self.shutdown_events[account.username]
            
            return None
            
        finally:
            # ВАЖНО: Всегда закрываем result_queue для предотвращения утечек ресурсов
            if result_queue is not None:
                try:
                    result_queue.close()
                    result_queue.join_thread()
                except Exception as cleanup_error:
                    logger.debug(f"Ошибка очистки result_queue: {cleanup_error}")
    
    # ==================== СПИСОК ЛОББИ ====================
    
    async def handle_list_lobbies(self, query):
        if not self.active_lobbies:
            await query.edit_message_text(
                "📋 <b>Нет активных лобби</b>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎮 Создать", callback_data="create_lobby"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                ]])
            )
            return
        
        message = "<b>📋 Активные лобби:</b>\n\n"
        keyboard = []
        
        for idx, (lobby_name, lobby) in enumerate(self.active_lobbies.items(), 1):
            message += f"✅ <b>{idx}. {lobby_name}</b>\n"
            message += f"🔒 Пароль: <code>{lobby.password}</code>\n"
            message += f"🤖 Бот: {lobby.account}\n"
            message += f"👥 Игроков: {lobby.players_count}/10\n\n"
            
            keyboard.append([
                InlineKeyboardButton(f"❌ Закрыть {idx}", callback_data=f"close_lobby_{lobby_name}")
            ])
        
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def handle_close_lobby(self, query, lobby_name: str):
        """Закрытие лобби и остановка процесса"""
        if lobby_name in self.active_lobbies:
            lobby = self.active_lobbies[lobby_name]
            
            # Останавливаем процесс Steam (если запущен)
            if lobby.account in self.active_processes:
                process = self.active_processes[lobby.account]
                try:
                    if process.is_alive():
                        logger.info(f"Останавливаем процесс для {lobby.account}, отправляем сигнал shutdown...")
                        
                        # Устанавливаем shutdown_event для graceful shutdown
                        if lobby.account in self.shutdown_events:
                            shutdown_event = self.shutdown_events[lobby.account]
                            shutdown_event.set()
                            logger.info(f"Ждём удаления лобби (макс 20 секунд)...")
                            
                            # Даём процессу 20 секунд на удаление лобби
                            process.join(timeout=20)
                        
                        # Если процесс не завершился - принудительно
                        if process.is_alive():
                            logger.warning(f"Процесс не завершился, принудительное завершение...")
                            process.terminate()
                            process.join(timeout=2)
                        
                        if process.is_alive():
                            logger.warning(f"Убиваем процесс...")
                            process.kill()
                            process.join(timeout=2)
                        
                        logger.info(f"✅ Процесс {lobby.account} остановлен")
                        
                except Exception as e:
                    logger.error(f"Ошибка остановки процесса: {e}")
                finally:
                    if lobby.account in self.active_processes:
                        del self.active_processes[lobby.account]
                    if lobby.account in self.shutdown_events:
                        del self.shutdown_events[lobby.account]
                    if lobby.account in self.result_queues:
                        del self.result_queues[lobby.account]
            
            # Освобождаем аккаунт
            for account in self.steam_accounts:
                if account.username == lobby.account:
                    account.is_busy = False
                    account.current_lobby = None
                    account.bot_instance = None
                    break
            
            # Удаляем лобби
            del self.active_lobbies[lobby_name]
            logger.info(f"✅ Лобби {lobby_name} закрыто")
            
            await query.answer("✅ Лобби закрыто!", show_alert=True)
            await self.handle_list_lobbies(query)
        else:
            await query.answer("❌ Лобби не найдено", show_alert=True)
    
    async def handle_destroy_all_lobbies(self, query):
        """Удаление ВСЕХ активных лобби"""
        if not self.active_lobbies:
            await query.answer("❌ Нет активных лобби", show_alert=True)
            return
        
        lobby_count = len(self.active_lobbies)
        
        # Показываем прогресс
        await query.edit_message_text(
            f"🔥 <b>Удаление всех лобби...</b>\n\n"
            f"Найдено лобби: {lobby_count}\n"
            f"⏳ Останавливаем процессы...",
            parse_mode='HTML'
        )
        
        import subprocess
        closed_count = 0
        
        # Копируем список лобби (чтобы избежать изменения во время итерации)
        lobbies_to_close = list(self.active_lobbies.items())
        
        for lobby_name, lobby in lobbies_to_close:
            try:
                # Останавливаем процесс Steam (если запущен)
                if lobby.account in self.active_processes:
                    process = self.active_processes[lobby.account]
                    try:
                        if process.is_alive():
                            logger.info(f"Останавливаем процесс для {lobby.account}, отправляем сигнал shutdown...")
                            
                            # Устанавливаем shutdown_event для graceful shutdown
                            if lobby.account in self.shutdown_events:
                                shutdown_event = self.shutdown_events[lobby.account]
                                shutdown_event.set()
                                logger.info(f"Ждём удаления лобби {lobby_name} (макс 20 секунд)...")
                                
                                # Даём процессу 20 секунд на удаление лобби
                                process.join(timeout=20)
                            
                            # Если процесс не завершился - принудительно
                            if process.is_alive():
                                logger.warning(f"Процесс {lobby.account} не завершился, принудительное завершение...")
                                process.terminate()
                                process.join(timeout=2)
                            
                            if process.is_alive():
                                logger.warning(f"Убиваем процесс {lobby.account}...")
                                process.kill()
                                process.join(timeout=2)
                            
                            logger.info(f"✅ Процесс {lobby.account} остановлен")
                            
                    except Exception as e:
                        logger.error(f"Ошибка остановки процесса {lobby.account}: {e}")
                    finally:
                        if lobby.account in self.active_processes:
                            del self.active_processes[lobby.account]
                        if lobby.account in self.shutdown_events:
                            del self.shutdown_events[lobby.account]
                        if lobby.account in self.result_queues:
                            del self.result_queues[lobby.account]
                
                # Освобождаем аккаунт
                for account in self.steam_accounts:
                    if account.username == lobby.account:
                        account.is_busy = False
                        account.current_lobby = None
                        account.bot_instance = None
                        break
                
                # Удаляем лобби
                if lobby_name in self.active_lobbies:
                    del self.active_lobbies[lobby_name]
                
                closed_count += 1
                logger.info(f"✅ Лобби {lobby_name} удалено ({closed_count}/{lobby_count})")
                
            except Exception as e:
                logger.error(f"Ошибка удаления лобби {lobby_name}: {e}")
        
        # Дополнительная очистка всех процессов steam/dota
        logger.info("🔪 Финальная очистка всех процессов...")
        try:
            subprocess.run(['pkill', '-9', '-f', 'steam'], stderr=subprocess.DEVNULL)
            subprocess.run(['pkill', '-9', '-f', 'dota'], stderr=subprocess.DEVNULL)
        except:
            pass
        
        # Показываем результат
        await query.edit_message_text(
            f"✅ <b>Все лобби удалены!</b>\n\n"
            f"🔥 Закрыто: {closed_count}\n"
            f"💚 Все боты освобождены\n"
            f"🧹 Процессы очищены",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="manage_bots")
            ]])
        )
    
    async def handle_cancel_creation(self, query, username: str):
        """Отмена создания лобби"""
        await query.answer("🛑 Отменяем создание...", show_alert=True)
        
        # Отправляем shutdown signal процессу
        if username in self.shutdown_events:
            logger.info(f"Отмена создания лобби для {username}")
            self.shutdown_events[username].set()
            
            # Останавливаем процесс
            if username in self.active_processes:
                process = self.active_processes[username]
                try:
                    if process.is_alive():
                        process.join(timeout=10)
                        if process.is_alive():
                            process.terminate()
                            process.join(timeout=2)
                        if process.is_alive():
                            process.kill()
                            process.join(timeout=2)
                except Exception as e:
                    logger.error(f"Ошибка остановки процесса при отмене: {e}")
                finally:
                    if username in self.active_processes:
                        del self.active_processes[username]
                    if username in self.shutdown_events:
                        del self.shutdown_events[username]
            
            # Освобождаем аккаунт
            for account in self.steam_accounts:
                if account.username == username:
                    account.is_busy = False
                    account.current_lobby = None
                    account.bot_instance = None
                    break
        
        # Возвращаемся в главное меню
        await query.edit_message_text(
            "❌ <b>Создание лобби отменено</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )
    
    # ==================== ОСТАЛЬНОЕ ====================
    
    async def handle_settings(self, query):
        message = f"""
<b>⚙️ Настройки</b>

📝 Базовое название: {self.lobby_base_name}
   (Лобби будут: {self.lobby_base_name} 1, {self.lobby_base_name} 2...)

🌍 Сервер: {self.server_region}
🎮 Режим: {self.game_mode}
👥 Зрители: Нет
🎯 Читы: Нет
        """
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )
    
    async def handle_schedule(self, query):
        """Меню управления расписанием матчей"""
        all_matches = self.schedule_config.get('matches', [])
        is_enabled = self.schedule_config.get('enabled', False)
        
        # Разделяем на запланированные и активные
        scheduled_matches = [m for m in all_matches if m.get('status') == 'scheduled']
        active_matches = [m for m in all_matches if m.get('status') == 'active']
        
        message = f"""
<b>📅 Расписание матчей</b>

<b>Статус:</b> {'🟢 Включено' if is_enabled else '🔴 Выключено'}
<b>Часовой пояс:</b> {self.schedule_config.get('timezone', 'Europe/Moscow')}

<b>📋 Запланировано:</b> {len(scheduled_matches)}
<b>🎮 Активных:</b> {len(active_matches)}
"""
        
        # Показываем запланированные матчи
        if scheduled_matches:
            message += "\n<b>📋 Предстоящие матчи:</b>\n"
            for idx, match in enumerate(scheduled_matches, 1):
                status_emoji = "✅" if match.get('enabled', False) else "⏸️"
                team1 = match.get('team1', '???')
                team2 = match.get('team2', '???')
                date = match.get('date', '???.??.????')
                time_str = match.get('time', '??:??')
                series = match.get('series_type', 'bo1').upper()
                mode = match.get('game_mode', 'CM')
                
                message += f"{status_emoji} <b>{idx}.</b> {team1} vs {team2}\n"
                message += f"     📅 {date} ⏰ {time_str} 🎯 {series} 🎮 {mode}\n"
        
        keyboard = []
        
        # Кнопки управления
        keyboard.append([
            InlineKeyboardButton("➕ Добавить матч", callback_data="match_add"),
            InlineKeyboardButton("📋 Добавить список", callback_data="match_add_list")
        ])
        
        if active_matches:
            keyboard.append([
                InlineKeyboardButton(f"🎮 Активные матчи ({len(active_matches)})", callback_data="match_view_active")
            ])
        
        if all_matches:
            keyboard.append([
                InlineKeyboardButton("✏️ Редактировать", callback_data="match_edit_menu"),
                InlineKeyboardButton("🗑️ Удалить всё", callback_data="match_delete_all")
            ])
        
        keyboard.append([
            InlineKeyboardButton(f"{'🔴 Выключить' if is_enabled else '🟢 Включить'}", 
                               callback_data="schedule_toggle_global"),
            InlineKeyboardButton("◀️ Назад", callback_data="back_main")
        ])
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    async def handle_view_active_matches(self, query):
        """Отображение активных матчей"""
        all_matches = self.schedule_config.get('matches', [])
        active_matches = [m for m in all_matches if m.get('status') == 'active']
        
        if not active_matches:
            await query.answer("Нет активных матчей", show_alert=True)
            return
        
        message = "<b>🎮 Активные матчи:</b>\n\n"
        
        for idx, match in enumerate(active_matches, 1):
            team1 = match.get('team1', '???')
            team2 = match.get('team2', '???')
            series = match.get('series_type', 'bo1').upper()
            mode = match.get('game_mode', 'CM')
            lobby_name = f"{team1} vs {team2}"
            
            message += f"<b>{idx}. {lobby_name}</b>\n"
            message += f"🎯 {series} | 🎮 {mode}\n"
            
            # Ищем информацию о лобби, если оно создано
            if lobby_name in self.active_lobbies:
                lobby = self.active_lobbies[lobby_name]
                message += f"🔒 Пароль: <code>{lobby.password}</code>\n"
                message += f"👥 Игроков: {lobby.players_count}/10\n"
            
            message += "\n"
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="schedule")
            ]])
        )
    
    async def handle_schedule_action(self, query, data: str):
        """Обработка действий с расписанием (только переключатель)"""
        if data == "schedule_toggle_global":
            # Включить/выключить всё расписание
            self.schedule_config['enabled'] = not self.schedule_config.get('enabled', False)
            self.save_schedule()
            
            # Перезапускаем планировщик
            self.setup_scheduler()
            
            await query.answer(
                f"✅ Расписание {'включено' if self.schedule_config['enabled'] else 'выключено'}!",
                show_alert=True
            )
            await self.handle_schedule(query)
    
    async def handle_match_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка действий с матчами"""
        query = update.callback_query
        data = query.data
        
        if data == "match_add":
            # Проверяем лимит матчей
            matches = self.schedule_config.get('matches', [])
            total_accounts = len(self.steam_accounts)
            
            if len(matches) >= total_accounts:
                await query.answer(
                    f"❌ Нельзя добавить больше {total_accounts} матчей!\n"
                    f"У вас только {total_accounts} аккаунтов Steam.\n"
                    f"Добавьте ещё аккаунты через 'Управление ботами'",
                    show_alert=True
                )
                return
            # Начало добавления матча
            await query.edit_message_text(
                "<b>➕ Добавление матча</b>\n\n"
                "Введите название первой команды:\n\n"
                "<b>Пример:</b> <code>team zxc</code>",
                parse_mode='HTML'
            )
            return WAITING_MATCH_TEAM1
        
        elif data == "match_add_list":
            # Добавление списка матчей
            await query.edit_message_text(
                "<b>📋 Добавление списка матчей</b>\n\n"
                "Введите список матчей (каждый с новой строки):\n\n"
                "<b>Формат:</b>\n"
                "<code>team zxc vs team asd время -18:00, дата 27.10.2025</code>\n"
                "<code>team abc vs team def время -19:30, дата 28.10.2025</code>\n\n"
                "<b>Важно:</b>\n"
                "• Каждый матч с новой строки\n"
                "• Время в формате HH:MM (часовой пояс МСК)\n"
                "• Дата в формате ДД.ММ.ГГГГ\n"
                "• По умолчанию: BO1, Captains Mode",
                parse_mode='HTML'
            )
            return WAITING_MATCH_LIST
        
        elif data == "match_delete_all":
            # Удалить все матчи
            self.schedule_config['matches'] = []
            self.save_schedule()
            await query.answer("✅ Все матчи удалены!", show_alert=True)
            await self.handle_schedule(query)
        
        elif data == "match_edit_menu":
            # Меню редактирования - показываем список матчей
            matches = self.schedule_config.get('matches', [])
            
            message = "<b>✏️ Редактирование матчей</b>\n\nВыберите матч:\n\n"
            keyboard = []
            
            for idx, match in enumerate(matches, 1):
                team1 = match.get('team1', '???')
                team2 = match.get('team2', '???')
                keyboard.append([
                    InlineKeyboardButton(
                        f"{idx}. {team1} vs {team2}",
                        callback_data=f"match_edit_{match['id']}"
                    )
                ])
            
            keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="schedule")])
            
            await query.edit_message_text(
                message,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif data.startswith("match_edit_"):
            # Редактирование конкретного матча
            match_id = int(data.replace("match_edit_", ""))
            context.user_data['editing_match_id'] = match_id
            
            matches = self.schedule_config.get('matches', [])
            match = next((m for m in matches if m.get('id') == match_id), None)
            
            if match:
                message = f"""
<b>✏️ Редактирование матча</b>

<b>Текущие данные:</b>
Команда 1: {match.get('team1')}
Команда 2: {match.get('team2')}
Дата: {match.get('date')}
Время: {match.get('time')}
Серия: {match.get('series_type', 'bo1').upper()}
Режим: {match.get('game_mode', 'Captains Mode')}

Введите название первой команды:
"""
                await query.edit_message_text(message, parse_mode='HTML')
                return WAITING_MATCH_TEAM1
    
    async def handle_match_team1_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод названия первой команды"""
        team1 = update.message.text.strip()
        context.user_data['match_team1'] = team1
        
        await update.message.reply_text(
            f"<b>✅ Команда 1:</b> {team1}\n\n"
            f"Введите название второй команды:\n"
            f"<i>Например: team asd</i>",
            parse_mode='HTML'
        )
        return WAITING_MATCH_TEAM2
    
    async def handle_match_team2_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод названия второй команды"""
        team2 = update.message.text.strip()
        context.user_data['match_team2'] = team2
        
        team1 = context.user_data.get('match_team1', '???')
        
        await update.message.reply_text(
            f"<b>✅ Команда 1:</b> {team1}\n"
            f"<b>✅ Команда 2:</b> {team2}\n\n"
            f"Введите дату матча:\n"
            f"<i>Формат: ДД.ММ.ГГГГ\nНапример: 26.10.2025</i>",
            parse_mode='HTML'
        )
        return WAITING_MATCH_DATE
    
    async def handle_match_date_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод даты матча"""
        date_str = update.message.text.strip()
        
        # Простая проверка формата
        import re
        if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
            await update.message.reply_text(
                "❌ Неверный формат даты!\n\n"
                "Используйте: <b>ДД.ММ.ГГГГ</b>\n"
                "Например: <b>26.10.2025</b>",
                parse_mode='HTML'
            )
            return WAITING_MATCH_DATE
        
        context.user_data['match_date'] = date_str
        
        await update.message.reply_text(
            f"<b>✅ Дата:</b> {date_str}\n\n"
            f"Введите время матча:\n"
            f"<i>Формат: ЧЧ:ММ\nНапример: 18:00</i>",
            parse_mode='HTML'
        )
        return WAITING_MATCH_TIME
    
    async def handle_match_time_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод времени матча"""
        time_str = update.message.text.strip()
        
        # Простая проверка формата
        import re
        if not re.match(r'^\d{1,2}:\d{2}$', time_str):
            await update.message.reply_text(
                "❌ Неверный формат времени!\n\n"
                "Используйте: <b>ЧЧ:ММ</b>\n"
                "Например: <b>18:00</b>",
                parse_mode='HTML'
            )
            return WAITING_MATCH_TIME
        
        context.user_data['match_time'] = time_str
        
        # Переход к выбору режима игры
        keyboard = [
            [InlineKeyboardButton("⚔️ Captains Mode", callback_data="match_mode_Captains Mode")],
            [InlineKeyboardButton("🎲 All Pick", callback_data="match_mode_All Pick")],
            [InlineKeyboardButton("📋 Captains Draft", callback_data="match_mode_Captains Draft")],
            [InlineKeyboardButton("🎯 Mid Only", callback_data="match_mode_Mid Only")],
            [InlineKeyboardButton("🥊 1v1 Solo Mid", callback_data="match_mode_1v1 Solo Mid")],
        ]
        
        await update.message.reply_text(
            f"<b>✅ Время:</b> {time_str}\n\n"
            f"Выберите режим игры:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_MATCH_GAME_MODE
    
    async def handle_match_list_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка ввода списка матчей"""
        import re
        from datetime import datetime
        
        text = update.message.text.strip()
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        if not lines:
            await update.message.reply_text(
                "❌ Список матчей пуст!\n\n"
                "Отправьте список матчей или /cancel для отмены.",
                parse_mode='HTML'
            )
            return WAITING_MATCH_LIST
        
        # Проверяем лимит
        total_accounts = len(self.steam_accounts)
        existing_matches = len(self.schedule_config.get('matches', []))
        
        if existing_matches + len(lines) > total_accounts:
            await update.message.reply_text(
                f"❌ Слишком много матчей!\n\n"
                f"У вас {total_accounts} аккаунтов\n"
                f"Уже запланировано: {existing_matches}\n"
                f"Вы пытаетесь добавить: {len(lines)}\n"
                f"Доступно мест: {total_accounts - existing_matches}\n\n"
                f"Удалите лишние строки или добавьте больше аккаунтов.",
                parse_mode='HTML'
            )
            return WAITING_MATCH_LIST
        
        # Парсим матчи
        added_matches = []
        errors = []
        
        # Паттерн: team1 vs team2 время -HH:MM, дата DD.MM.YYYY
        pattern = r'^(.+?)\s+vs\s+(.+?)\s+время\s*-\s*(\d{1,2}:\d{2})\s*,\s*дата\s+(\d{2}\.\d{2}\.\d{4})$'
        
        for line_num, line in enumerate(lines, 1):
            match = re.search(pattern, line, re.IGNORECASE)
            
            if not match:
                errors.append(f"Строка {line_num}: неверный формат")
                continue
            
            team1 = match.group(1).strip()
            team2 = match.group(2).strip()
            time_str = match.group(3).strip()
            date_str = match.group(4).strip()
            
            # Проверяем дату
            try:
                date_obj = datetime.strptime(date_str, '%d.%m.%Y')
            except:
                errors.append(f"Строка {line_num}: неверная дата '{date_str}'")
                continue
            
            # Проверяем время
            if not re.match(r'^\d{1,2}:\d{2}$', time_str):
                errors.append(f"Строка {line_num}: неверное время '{time_str}'")
                continue
            
            # Создаём матч (настройки будут добавлены после выбора)
            match_data = {
                'id': int(datetime.now().timestamp() * 1000 + line_num),  # Уникальный ID
                'team1': team1,
                'team2': team2,
                'date': date_str,
                'time': time_str,
            }
            
            added_matches.append(match_data)
        
        # Если есть ошибки и нет успешных матчей - показываем ошибки и выходим
        if errors and not added_matches:
            error_message = "<b>❌ Не удалось распарсить матчи!</b>\n\n"
            for error in errors[:10]:
                error_message += f"• {error}\n"
            if len(errors) > 10:
                error_message += f"• ... и ещё {len(errors) - 10}\n"
            error_message += "\n<b>Проверьте формат:</b>\n"
            error_message += "<code>team1 vs team2 время -18:00, дата 27.10.2025</code>"
            
            await update.message.reply_text(
                error_message,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К расписанию", callback_data="schedule")
                ]])
            )
            return ConversationHandler.END
        
        # Сохраняем распарсенные матчи во временное хранилище
        context.user_data['bulk_matches'] = added_matches
        context.user_data['bulk_errors'] = errors
        
        # Показываем сводку и переходим к выбору режима игры
        summary = f"<b>✅ Распознано матчей: {len(added_matches)}</b>\n\n"
        for m in added_matches[:5]:  # Показываем первые 5
            summary += f"• {m['team1']} vs {m['team2']}\n"
            summary += f"  📅 {m['date']} ⏰ {m['time']}\n"
        if len(added_matches) > 5:
            summary += f"• ... и ещё {len(added_matches) - 5}\n"
        
        if errors:
            summary += f"\n⚠️ <b>Ошибок:</b> {len(errors)}\n"
        
        summary += "\n<b>Выберите режим игры для всех матчей:</b>"
        
        keyboard = [
            [InlineKeyboardButton("⚔️ Captains Mode", callback_data="match_mode_Captains Mode")],
            [InlineKeyboardButton("🎲 All Pick", callback_data="match_mode_All Pick")],
            [InlineKeyboardButton("📋 Captains Draft", callback_data="match_mode_Captains Draft")],
            [InlineKeyboardButton("🎯 Mid Only", callback_data="match_mode_Mid Only")],
            [InlineKeyboardButton("🥊 1v1 Solo Mid", callback_data="match_mode_1v1 Solo Mid")],
        ]
        
        await update.message.reply_text(
            summary,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return WAITING_MATCH_GAME_MODE
    
    async def handle_match_mode_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор режима игры для матча"""
        query = update.callback_query
        game_mode = query.data.replace("match_mode_", "")
        context.user_data['match_game_mode'] = game_mode
        
        # Переход к выбору серии
        keyboard = [
            [InlineKeyboardButton("1️⃣ Одна игра (BO1)", callback_data="match_series_bo1")],
            [InlineKeyboardButton("2️⃣ Две игры (BO2)", callback_data="match_series_bo2")],
            [InlineKeyboardButton("3️⃣ До 2 побед (BO3)", callback_data="match_series_bo3")],
            [InlineKeyboardButton("5️⃣ До 3 побед (BO5)", callback_data="match_series_bo5")],
        ]
        
        await query.edit_message_text(
            f"<b>✅ Режим:</b> {game_mode}\n\n"
            f"Выберите тип серии:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAITING_MATCH_SERIES
    
    async def handle_match_series_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор серии для матча и сохранение"""
        query = update.callback_query
        series_type = query.data.replace("match_series_", "")
        context.user_data['match_series_type'] = series_type
        game_mode = context.user_data.get('match_game_mode')
        
        # Проверяем, это массовое добавление или одиночное
        bulk_matches = context.user_data.get('bulk_matches')
        
        if bulk_matches:
            # МАССОВОЕ добавление матчей из списка
            bulk_errors = context.user_data.get('bulk_errors', [])
            
            # Применяем настройки ко всем матчам
            for match in bulk_matches:
                match['game_mode'] = game_mode
                match['series_type'] = series_type
                match['enabled'] = True
                match['status'] = 'scheduled'
            
            # Сохраняем все матчи
            if 'matches' not in self.schedule_config:
                self.schedule_config['matches'] = []
            
            self.schedule_config['matches'].extend(bulk_matches)
            self.save_schedule()
            self.setup_scheduler()
            
            # Формируем отчёт
            result_message = "<b>✅ Матчи добавлены в расписание!</b>\n\n"
            result_message += f"<b>Режим игры:</b> {game_mode}\n"
            result_message += f"<b>Серия:</b> {series_type.upper()}\n"
            result_message += f"<b>Добавлено матчей:</b> {len(bulk_matches)}\n\n"
            
            for m in bulk_matches[:5]:
                result_message += f"• {m['team1']} vs {m['team2']}\n"
                result_message += f"  📅 {m['date']} ⏰ {m['time']}\n"
            if len(bulk_matches) > 5:
                result_message += f"• ... и ещё {len(bulk_matches) - 5}\n"
            
            if bulk_errors:
                result_message += f"\n⚠️ <b>Строк с ошибками:</b> {len(bulk_errors)}"
            
            await query.edit_message_text(
                result_message,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К расписанию", callback_data="schedule")
                ]])
            )
            
            # Очищаем временные данные
            if 'bulk_matches' in context.user_data:
                del context.user_data['bulk_matches']
            if 'bulk_errors' in context.user_data:
                del context.user_data['bulk_errors']
            if 'match_game_mode' in context.user_data:
                del context.user_data['match_game_mode']
            
            return ConversationHandler.END
        
        # ОДИНОЧНОЕ добавление матча
        # Собираем все данные
        team1 = context.user_data.get('match_team1')
        team2 = context.user_data.get('match_team2')
        date = context.user_data.get('match_date')
        time_str = context.user_data.get('match_time')
        
        # Проверяем, редактируем или создаём
        editing_match_id = context.user_data.get('editing_match_id')
        
        if editing_match_id:
            # Редактирование существующего матча
            matches = self.schedule_config.get('matches', [])
            for match in matches:
                if match.get('id') == editing_match_id:
                    match['team1'] = team1
                    match['team2'] = team2
                    match['date'] = date
                    match['time'] = time_str
                    match['game_mode'] = game_mode
                    match['series_type'] = series_type
                    break
            
            del context.user_data['editing_match_id']
            message = "✅ <b>Матч обновлён!</b>\n\n"
        else:
            # Создание нового матча
            new_id = max([m.get('id', 0) for m in self.schedule_config.get('matches', [])], default=0) + 1
            
            new_match = {
                'id': new_id,
                'team1': team1,
                'team2': team2,
                'date': date,
                'time': time_str,
                'game_mode': game_mode,
                'series_type': series_type,
                'enabled': True,
                'status': 'scheduled'
            }
            
            if 'matches' not in self.schedule_config:
                self.schedule_config['matches'] = []
            
            self.schedule_config['matches'].append(new_match)
            message = "✅ <b>Матч добавлен!</b>\n\n"
        
        self.save_schedule()
        
        message += f"<b>{team1} vs {team2}</b>\n"
        message += f"📅 {date} ⏰ {time_str}\n"
        message += f"🎮 {game_mode}\n"
        message += f"🎯 {series_type.upper()}"
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ К расписанию", callback_data="schedule")
            ]])
        )
        
        # Очищаем временные данные
        for key in ['match_team1', 'match_team2', 'match_date', 'match_time', 'match_game_mode', 'match_series_type']:
            context.user_data.pop(key, None)
        
        return ConversationHandler.END
    
    async def handle_status(self, query):
        total = len(self.steam_accounts)
        available = len(self.get_available_accounts())
        
        message = f"""
<b>📊 Статус</b>

🤖 Боты:
   Всего: {total}
   💚 Свободных: {available}
   🔴 Занятых: {total - available}

🎯 Лобби: {len(self.active_lobbies)}
        """
        try:
            await query.edit_message_text(
                message,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Обновить", callback_data="status"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                ]])
            )
        except:
            pass
    
    async def handle_back_to_main(self, query):
        text = self.get_welcome_text()
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=self.get_main_keyboard())
        except:
            pass
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    # ==================== ПЛАНИРОВЩИК ====================
    
    def setup_scheduler(self):
        """Настройка планировщика для автоматического создания лобби"""
        if self.scheduler is None:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            self.scheduler = AsyncIOScheduler(timezone=self.schedule_config.get('timezone', 'Europe/Moscow'))
        
        # Очищаем старые задачи
        self.scheduler.remove_all_jobs()
        
        # ВАЖНО: Добавляем задачу мониторинга активных лобби (работает всегда, независимо от расписания)
        self.scheduler.add_job(
            self.monitor_active_lobbies,
            'interval',
            seconds=10,
            id='monitor_lobbies',
            replace_existing=True
        )
        logger.info("👁️ Добавлена задача мониторинга активных лобби (каждые 10 сек)")
        
        # Если расписание выключено - не добавляем задачи матчей
        if not self.schedule_config.get('enabled', False):
            logger.info("📅 Расписание выключено, задачи матчей не добавлены")
            return
        
        # Добавляем задачи для каждого активного матча
        matches = self.schedule_config.get('matches', [])
        active_matches = [m for m in matches if m.get('enabled', False)]
        
        for match in active_matches:
            try:
                # Парсим дату и время
                date_str = match.get('date')  # "26.10.2025"
                time_str = match.get('time')  # "18:00"
                
                day, month, year = map(int, date_str.split('.'))
                hour, minute = map(int, time_str.split(':'))
                
                # Создаем задачу на конкретную дату и время
                run_date = datetime(year, month, day, hour, minute)
                
                # Добавляем задачу
                self.scheduler.add_job(
                    self.execute_scheduled_match,
                    'date',
                    run_date=run_date,
                    args=[match],
                    id=f"match_{match['id']}",
                    replace_existing=True,
                    max_instances=10  # Разрешаем до 10 одновременных создания лобби
                )
                
                logger.info(f"📅 Добавлена задача: {match['team1']} vs {match['team2']} на {date_str} {time_str}")
            
            except Exception as e:
                logger.error(f"Ошибка добавления задачи для матча {match.get('id')}: {e}")
        
        # Запускаем планировщик если есть задачи (только если event loop уже запущен)
        if active_matches or True:  # Запускаем всегда для мониторинга
            if not self.scheduler.running:
                try:
                    self.scheduler.start()
                    logger.info(f"✅ Планировщик запущен, задач: {len(active_matches)}")
                except RuntimeError:
                    # Event loop еще не запущен, планировщик запустится позже
                    logger.info(f"📅 Планировщик будет запущен при старте event loop, задач: {len(active_matches)}")
        else:
            logger.info("📅 Нет активных матчей в расписании")
    
    async def execute_scheduled_match(self, match: dict):
        """Выполнение создания лобби для запланированного матча"""
        try:
            team1 = match.get('team1')
            team2 = match.get('team2')
            game_mode = match.get('game_mode', 'Captains Mode')
            series_type = match.get('series_type', 'bo1')
            
            lobby_name = f"{team1} vs {team2}"
            
            logger.info(f"🎮 Создание лобби по расписанию: {lobby_name}")
            
            # Ищем свободный аккаунт
            available_accounts = self.get_available_accounts()
            
            if not available_accounts:
                logger.error(f"❌ Нет свободных аккаунтов для создания лобби: {lobby_name}")
                
                # Отправляем уведомление в Telegram
                if self.notification_chat_id:
                    message = f"❌ <b>Ошибка создания лобби по расписанию!</b>\n\n"
                    message += f"<b>{lobby_name}</b>\n"
                    message += f"Причина: нет свободных аккаунтов"
                    
                    send_kwargs = {
                        'chat_id': self.notification_chat_id,
                        'text': message,
                        'parse_mode': 'HTML'
                    }
                    
                    if self.notification_thread_id:
                        send_kwargs['message_thread_id'] = self.notification_thread_id
                    
                    await self.telegram_app.bot.send_message(**send_kwargs)
                
                return
            
            # Берем первый свободный аккаунт
            account = available_accounts[0]
            account.is_busy = True
            
            # Создаем фейковый status_msg для create_single_real_lobby
            class FakeMessage:
                async def edit_text(self, *args, **kwargs):
                    pass
            
            fake_msg = FakeMessage()
            
            # Создаем лобби
            lobby_info = await self.create_single_real_lobby(
                account,
                fake_msg,
                game_mode=game_mode,
                series_type=series_type,
                lobby_name=lobby_name
            )
            
            if lobby_info:
                logger.info(f"✅ Лобби создано по расписанию: {lobby_name}")
                
                # Меняем статус матча на "active"
                match['status'] = 'active'
                self.save_schedule()
                
                # Отправляем уведомление в личку администратору
                for admin_id in self.admin_ids:
                    admin_message = f"✅ <b>Лобби создано по расписанию!</b>\n\n"
                    admin_message += f"<b>{lobby_name}</b>\n"
                    admin_message += f"🔒 Пароль: <code>{lobby_info.password}</code>\n"
                    admin_message += f"🎮 Режим: {game_mode}\n"
                    admin_message += f"🎯 Серия: {series_type.upper()}"
                    
                    try:
                        await self.telegram_app.bot.send_message(
                            chat_id=admin_id,
                            text=admin_message,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
                
                # Отправляем уведомление в группу
                if self.notification_chat_id:
                    group_message = f"<b>{lobby_name}</b>\n\n"
                    group_message += f"<b>🔒 Пароль: </b><code>{lobby_info.password}</code>\n"
                    group_message += f"<b>🎮 Режим: {game_mode}</b>\n"
                    group_message += f"<b>🎯 Серия: {series_type.upper()}</b>"
                    
                    send_kwargs = {
                        'chat_id': self.notification_chat_id,
                        'text': group_message,
                        'parse_mode': 'HTML'
                    }
                    
                    if self.notification_thread_id:
                        send_kwargs['message_thread_id'] = self.notification_thread_id
                    
                    await self.telegram_app.bot.send_message(**send_kwargs)
            else:
                logger.error(f"❌ Не удалось создать лобби по расписанию: {lobby_name}")
                account.is_busy = False
                
                # Отправляем уведомление об ошибке
                if self.notification_chat_id:
                    message = f"❌ <b>Ошибка создания лобби по расписанию!</b>\n\n"
                    message += f"<b>{lobby_name}</b>\n"
                    message += f"Проверьте логи для деталей"
                    
                    send_kwargs = {
                        'chat_id': self.notification_chat_id,
                        'text': message,
                        'parse_mode': 'HTML'
                    }
                    
                    if self.notification_thread_id:
                        send_kwargs['message_thread_id'] = self.notification_thread_id
                    
                    await self.telegram_app.bot.send_message(**send_kwargs)
        
        except Exception as e:
            logger.error(f"Ошибка выполнения запланированного матча: {e}", exc_info=True)
    
    def _cleanup_lobby_for_username(self, username: str):
        """Очищает все данные для указанного username и обновляет статус в Telegram"""
        try:
            # Находим аккаунт и лобби
            for account in self.steam_accounts:
                if account.username == username:
                    lobby_name = account.current_lobby
                    
                    logger.info(f"🔄 Обновление статуса: аккаунт {username}, лобби {lobby_name}")
                    
                    # Освобождаем аккаунт
                    account.is_busy = False
                    account.current_lobby = None
                    
                    # Удаляем лобби из активных
                    if lobby_name and lobby_name in self.active_lobbies:
                        del self.active_lobbies[lobby_name]
                        logger.info(f"✅ Лобби {lobby_name} удалено из активных")
                    else:
                        if lobby_name:
                            logger.warning(f"⚠️ Лобби {lobby_name} не найдено в active_lobbies")
                    
                    # Очищаем процесс
                    if username in self.active_processes:
                        del self.active_processes[username]
                        logger.info(f"🧹 Процесс {username} удален")
                    if username in self.shutdown_events:
                        del self.shutdown_events[username]
                    if username in self.result_queues:
                        del self.result_queues[username]
                    
                    logger.info(f"✅ Статус бота {username} успешно обновлен в Telegram!")
                    break
        except Exception as e:
            logger.error(f"❌ Ошибка очистки для {username}: {e}", exc_info=True)
    
    async def monitor_active_lobbies(self):
        """Периодически проверяет активные лобби на предмет завершения игры"""
        try:
            active_count = len(self.result_queues)
            if active_count > 0:
                logger.info(f"👁️ Мониторинг: проверка {active_count} активных лобби...")
            
            for username, queue in list(self.result_queues.items()):
                try:
                    # Проверяем, жив ли процесс (вместо проверки очереди!)
                    process = self.active_processes.get(username)
                    if process and not process.is_alive():
                        logger.info(f"💀 Процесс {username} завершился - обновляем статус")
                        # Процесс завершился, обновляем статус
                        self._cleanup_lobby_for_username(username)
                        continue
                    
                    # Проверяем очередь без блокировки (только если процесс жив!)
                    if not queue.empty():
                        result = queue.get_nowait()
                        logger.info(f"📨 Получено сообщение из очереди для {username}: {result}")
                        
                        # Если получили сообщение о закрытии лобби
                        if result.get('lobby_closed'):
                            logger.info(f"🏁 Лобби для {username} закрылось (игра завершена)")
                            self._cleanup_lobby_for_username(username)
                except Exception as queue_error:
                    logger.error(f"❌ Ошибка проверки очереди для {username}: {queue_error}", exc_info=True)
        except Exception as e:
            logger.error(f"❌ Критическая ошибка мониторинга лобби: {e}", exc_info=True)
    
    # ==================== SETUP ====================
    
    async def post_init(self, application: Application) -> None:
        """Вызывается после инициализации Application"""
        # Запускаем планировщик если он не запущен и есть задачи
        if self.scheduler and not self.scheduler.running:
            if self.scheduler.get_jobs():
                self.scheduler.start()
                logger.info(f"✅ Планировщик запущен в post_init, задач: {len(self.scheduler.get_jobs())}")
    
    def setup_telegram_bot(self):
        self.telegram_app = Application.builder().token(self.telegram_token).post_init(self.post_init).build()
        
        # Handler создания лобби с выбором ботов, режима и серии
        create_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.handle_create_lobby_request, pattern="^create_lobby$")],
            states={
                WAITING_SELECT_BOTS: [
                    CallbackQueryHandler(self.handle_toggle_bot_selection, pattern="^toggle_bot_"),
                    CallbackQueryHandler(self.handle_confirm_bot_selection, pattern="^confirm_bot_selection$"),
                ],
                WAITING_GAME_MODE: [
                    CallbackQueryHandler(self.handle_game_mode_selection, pattern="^mode_"),
                ],
                WAITING_SERIES_TYPE: [
                    CallbackQueryHandler(self.handle_series_selection, pattern="^series_"),
                ],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
            allow_reentry=True
        )
        
        # Handler добавления бота
        add_bot_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.handle_add_bot_request, pattern="^add_bot$")],
            states={
                WAITING_ACCOUNT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_account_data_input)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
            allow_reentry=True
        )
        
        # Handler редактирования бота
        edit_bot_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.handle_edit_bot_request, pattern="^edit_bot_")],
            states={
                WAITING_EDIT_BOT_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_edit_bot_data_input)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
            allow_reentry=True
        )
        
        # Handler управления матчами (добавление и редактирование)
        match_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.handle_match_action, pattern="^match_add$"),
                CallbackQueryHandler(self.handle_match_action, pattern="^match_add_list$"),
                CallbackQueryHandler(self.handle_match_action, pattern="^match_edit_"),
            ],
            states={
                WAITING_MATCH_TEAM1: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_match_team1_input)],
                WAITING_MATCH_TEAM2: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_match_team2_input)],
                WAITING_MATCH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_match_date_input)],
                WAITING_MATCH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_match_time_input)],
                WAITING_MATCH_LIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_match_list_input)],
                WAITING_MATCH_GAME_MODE: [CallbackQueryHandler(self.handle_match_mode_selection, pattern="^match_mode_")],
                WAITING_MATCH_SERIES: [CallbackQueryHandler(self.handle_match_series_selection, pattern="^match_series_")],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
            allow_reentry=True
        )
        
        self.telegram_app.add_handler(CommandHandler("start", self.cmd_start))
        self.telegram_app.add_handler(create_handler)
        self.telegram_app.add_handler(add_bot_handler)
        self.telegram_app.add_handler(edit_bot_handler)
        self.telegram_app.add_handler(match_handler)
        self.telegram_app.add_handler(CallbackQueryHandler(self.button_callback))
    
    def shutdown_all_lobbies(self, signum=None, frame=None):
        """Graceful shutdown - удаляет все активные лобби"""
        logger.info("=" * 50)
        logger.info("🛑 Получен сигнал завершения работы, закрываем все лобби...")
        logger.info("=" * 50)
        
        # Отправляем shutdown signal всем активным процессам
        for username, event in list(self.shutdown_events.items()):
            logger.info(f"Отправка shutdown signal для {username}...")
            event.set()
        
        # Ждем завершения всех процессов (даём время на cleanup)
        import time
        time.sleep(1)  # Даём время процессам получить сигнал
        
        for username, process in list(self.active_processes.items()):
            if process.is_alive():
                logger.info(f"Ожидание завершения процесса {username}...")
                process.join(timeout=20)  # Увеличено до 20 секунд
                
                if process.is_alive():
                    logger.warning(f"⚠️ Процесс {username} не завершился, принудительное завершение...")
                    process.terminate()
                    process.join(timeout=5)
        
        logger.info("✅ Все лобби закрыты")
    
    def start_sync(self):
        logger.info("=" * 50)
        logger.info("🚀 REAL Dota 2 Lobby Bot v2")
        logger.info("=" * 50)
        
        if not self.telegram_token:
            logger.error("❌ Нет TELEGRAM_BOT_TOKEN")
            return
        
        logger.info("Настройка...")
        self.setup_telegram_bot()
        self.setup_scheduler()
        
        # Регистрируем обработчики сигналов для graceful shutdown
        signal.signal(signal.SIGINT, self.shutdown_all_lobbies)
        signal.signal(signal.SIGTERM, self.shutdown_all_lobbies)
        
        logger.info(f"Аккаунтов: {len(self.steam_accounts)}")
        logger.info("✅ Бот запущен!")
        logger.info("=" * 50)
        
        try:
            self.telegram_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        finally:
            # На случай если polling завершился без сигнала
            self.shutdown_all_lobbies()


def main():
    # Настройка multiprocessing для Windows/Linux
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass  # Уже установлен
    
    bot = RealDota2BotV2()
    try:
        bot.start_sync()
    except KeyboardInterrupt:
        logger.info("⏹️ Остановка бота...")
        
        # Отправляем сигнал shutdown всем процессам
        for username in list(bot.shutdown_events.keys()):
            if username in bot.shutdown_events:
                logger.info(f"Отправляем сигнал shutdown для {username}...")
                bot.shutdown_events[username].set()
        
        # Ждём завершения всех процессов (макс 25 секунд)
        logger.info("Ожидание завершения всех процессов (макс 25 секунд)...")
        for username, process in list(bot.active_processes.items()):
            try:
                if process.is_alive():
                    process.join(timeout=25)
                    
                    if process.is_alive():
                        logger.warning(f"Процесс {username} не завершился, принудительное завершение...")
                        process.terminate()
                        process.join(timeout=2)
                    
                    if process.is_alive():
                        logger.warning(f"Убиваем {username}...")
                        process.kill()
                        process.join()
            except Exception as e:
                logger.error(f"Ошибка остановки процесса {username}: {e}")
        
        # УБИВАЕМ ВСЕ ОСТАВШИЕСЯ ПРОЦЕССЫ Steam/Dota/Python
        logger.info("🔪 Очистка всех оставшихся процессов...")
        import subprocess
        try:
            subprocess.run(['pkill', '-9', '-f', 'steam'], stderr=subprocess.DEVNULL)
            subprocess.run(['pkill', '-9', '-f', 'dota'], stderr=subprocess.DEVNULL)
        except:
            pass
        
        logger.info("✅ Все процессы остановлены")
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)


if __name__ == "__main__":
    main()

