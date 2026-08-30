#!/bin/bash

# Остановка скрипта при ошибках
set -e

echo "=== Настройка Голосового Ассистента ==="

# 1. Определение текущей директории
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[+] Директория проекта: $PROJECT_DIR"

# 2. Установка системных пакетов Manjaro (pacman)
echo "[+] Проверка и установка системных зависимостей Arch/Manjaro..."
sudo pacman -Syu --needed --noconfirm python python-pip portaudio alsa-utils git

# 3. Создание структуры папок
echo "[+] Создание необходимых директорий..."
mkdir -p "$PROJECT_DIR/models"
mkdir -p "$PROJECT_DIR/skills"
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$HOME/.config/systemd/user"

# 4. Генерация файла requirements.txt
echo "[+] Создание requirements.txt..."
cat << 'EOF' > "$PROJECT_DIR/requirements.txt"
vosk
sounddevice
python-dotenv
PySide6
scikit-learn
groq
opencv-python
wikipedia-api
requests
aiohttp
EOF

# 5. Создание шаблона .env файла, если он не существует
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[+] Создание файла .env (шаблон)..."
    cat << 'EOF' > "$PROJECT_DIR/.env"
GROQ_API_KEY=your_groq_api_key_here
MODEL_NAME=llama3-8b-8192
EOF
    echo "    (!) Не забудьте указать ваш GROQ_API_KEY в файле .env!"
fi

# 6. Создание Виртуального окружения Python (venv)
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "[+] Создание виртуального окружения venv..."
    python -m venv "$PROJECT_DIR/venv"
fi

echo "[+] Установка Python-пакетов в venv..."
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 7. Настройка systemd-сервиса автозапуска
echo "[+] Настройка службы systemd (voice-assistant.service)..."
SERVICE_FILE="$HOME/.config/systemd/user/voice-assistant.service"

cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Voice Assistant Service
After=network.target sound.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/ai_chat.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

# Перезагрузка и активация сервиса
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service

echo ""
echo "=================================================="
echo "Установка успешно завершена!"
echo "=================================================="
echo "Запустить ассистента прямо сейчас можно командой:"
echo "  systemctl --user start voice-assistant.service"
echo ""
echo "Проверить статус работы:"
echo "  systemctl --user status voice-assistant.service"
echo "=================================================="
