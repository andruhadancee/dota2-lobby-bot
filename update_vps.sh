#!/bin/bash
# Скрипт автоматического обновления бота на VPS

echo "🔄 Автоматическое обновление бота..."
echo "===================================="

# 1. Останавливаем старого бота
echo "⏹️  Останавливаем старого бота..."
pkill -9 python3
sleep 2

# 2. Скачиваем обновлённый код
echo "📥 Скачиваем обновления..."
cd ~/dota2_lobby_bot
git pull

# 3. Проверяем и обновляем .env файл
echo "🔧 Проверяем .env файл..."

# Проверяем наличие NOTIFICATION_CHAT_ID
if ! grep -q "NOTIFICATION_CHAT_ID" .env; then
    echo "➕ Добавляем NOTIFICATION_CHAT_ID..."
    echo "" >> .env
    echo "# Уведомления в группу" >> .env
    echo "NOTIFICATION_CHAT_ID=-1003106019461" >> .env
else
    echo "✅ NOTIFICATION_CHAT_ID уже есть"
fi

# Проверяем наличие NOTIFICATION_THREAD_ID
if ! grep -q "NOTIFICATION_THREAD_ID" .env; then
    echo "➕ Добавляем NOTIFICATION_THREAD_ID..."
    echo "NOTIFICATION_THREAD_ID=37" >> .env
else
    echo "✅ NOTIFICATION_THREAD_ID уже есть"
fi

# 4. Активируем venv и запускаем бота
echo "🚀 Запускаем обновлённого бота..."
source venv/bin/activate
python3 main.py

echo "===================================="
echo "✅ Обновление завершено!"

