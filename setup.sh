#!/bin/bash
# ==============================================================================
# setup.sh — Скрипт автоматической установки и настройки ассистента Джарвис
# Платформа: Manjaro Linux (GNOME / PipeWire / Wayland / XWayland)
# ==============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/voice-assistant.service"
UID_NUM="$(id -u)"

echo "=================================================================="
echo "    🚀 Установка и настройка голосового ассистента «Джарвис»     "
echo "=================================================================="
echo "[+] Каталог проекта: $PROJECT_DIR"

# 1. Установка системных пакетов
echo "[+] Шаг 1/7: Установка системных зависимостей через pacman..."
CORE_PKGS=(
    python python-pip
    portaudio alsa-utils
    git wget unzip tar
    pipewire pipewire-pulse wireplumber libpulse webrtc-audio-processing
    audacious audacious-plugins
    mpv yt-dlp ffmpeg
    cmatrix
    gnome-terminal nautilus gnome-system-monitor
    htop neofetch
    libxcb tk xorg-xhost
    xdotool wmctrl
)
sudo pacman -Syu --needed --noconfirm "${CORE_PKGS[@]}"

for opt in telegram-desktop; do
    if pacman -Si "$opt" >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm "$opt" || echo "[!] Пакет $opt пропущен."
    fi
done

# 2. Создание каталогов
echo "[+] Шаг 2/7: Подготовка каталогов..."
mkdir -p "$PROJECT_DIR/model"
mkdir -p "$PROJECT_DIR/piper/models"
mkdir -p "$PROJECT_DIR/.tts_cache"
mkdir -p "$HOME/.scripts"
mkdir -p "$SERVICE_DIR"

# 3. Настройка файла конфигурации (.env)
echo "[+] Шаг 3/7: Проверка конфигурации .env..."
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
if [ ! -f "$ENV_EXAMPLE" ]; then
    cat << 'EOF' > "$ENV_EXAMPLE"
# Groq — облачный диалог и аналитика ИИ
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b

# T-Invest — навык «Биржа и портфель»
# Голосовые сделки — сразу. Фон сам торгует только при TINKOFF_AUTO_TRADE=true.
# Пустой флаг — автоторговля лишь в песочнице (TINKOFF_SANDBOX=true).
TINKOFF_TOKEN=
TINKOFF_ACCOUNT_ID=
TINKOFF_SANDBOX=false
TINKOFF_AUTO_TRADE=
TINKOFF_WATCHLIST=SBER,LKOH,YDEX,VTBR

# Telegram-бот: управление и снимки охраны
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Охрана: камера OpenCV (число или /dev/videoN)
# CAMERA_INDEX=0

# Голос синтеза Piper TTS: ru_RU-dmitri-medium.onnx
PIPER_MODEL=ru_RU-dmitri-medium.onnx
VOICE_SPEED=1.0

# Погода по умолчанию: город (геокодинг) и/или точные координаты.
# Если заданы DEFAULT_LAT и DEFAULT_LON, «какая погода» без города идёт в эту точку.
# DEFAULT_CITY тогда только имя вслух; пустое имя озвучивается как «здесь».
DEFAULT_CITY=Москва
# DEFAULT_LAT=
# DEFAULT_LON=

# Лампа Xiaomi / Yeelight (опционально)
XIAOMI_BULB_IP=
XIAOMI_BULB_TOKEN=

# Home Assistant (умный дом, тумблер в настройках)
HA_URL=http://127.0.0.1:8123
HA_TOKEN=

# Сессия внимания (в секундах)
ATTENTION_TIMEOUT=6
ATTENTION_TIMEOUT_MUSIC=3

# Настройки приглушения звука (Ducking)
DUCKING_VOLUME=8
DUCKING_MODE=duck
MIN_SPEECH_RMS=200

# Поиск и карты: yandex или google
SEARCH_PROVIDER=yandex
MAPS_PROVIDER=yandex
AI_PROVIDER=yandex

# Консольный режим и GUI
CONSOLE_MODE=false
GUI_ENABLED=true
EOF
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[+] Копирую .env.example -> .env"
    cp "$ENV_EXAMPLE" "$PROJECT_DIR/.env"
else
    echo "[+] Файл .env уже существует — пропускаю перезапись."
fi

# 4. Создание виртуального окружения Python
echo "[+] Шаг 4/7: Настройка виртуального окружения Python..."
if [ ! -d "$VENV_DIR" ]; then
    python -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 5. Загрузка модели распознавания речи Vosk
echo "[+] Шаг 5/7: Проверка оффлайн-модели Vosk..."
if [ ! -d "$PROJECT_DIR/model/am" ]; then
    echo "[+] Скачиваю русскую модель Vosk (small-ru-0.22)..."
    tmpdir="$(mktemp -d)"
    (
        cd "$tmpdir"
        wget -q --show-progress "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
        unzip -q vosk-model-small-ru-0.22.zip
        cp -a vosk-model-small-ru-0.22/. "$PROJECT_DIR/model/"
    )
    rm -rf "$tmpdir"
else
    echo "[+] Модель Vosk уже установлена."
fi

# 6. Загрузка движка и модели голоса Piper TTS
echo "[+] Шаг 6/7: Проверка движка и моделей Piper TTS..."
if [ ! -x "$PROJECT_DIR/piper/piper" ]; then
    echo "[+] Скачиваю бинарник Piper TTS..."
    tmpdir="$(mktemp -d)"
    (
        cd "$tmpdir"
        wget -q --show-progress "https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz"
        tar -xf piper_amd64.tar.gz
        cp -a piper/. "$PROJECT_DIR/piper/"
    )
    rm -rf "$tmpdir"
    chmod +x "$PROJECT_DIR/piper/piper"
else
    echo "[+] Бинарник Piper уже установлен."
fi

PIPER_ONNX="$PROJECT_DIR/piper/models/ru_RU-dmitri-medium.onnx"
PIPER_JSON="$PIPER_ONNX.json"
if [ ! -s "$PIPER_ONNX" ]; then
    echo "[+] Скачиваю русскую голосовую модель Dmitri (Piper ONNX)..."
    wget -q --show-progress -O "$PIPER_ONNX" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx"
    wget -q --show-progress -O "$PIPER_JSON" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json"
else
    echo "[+] Голосовая модель Dmitri уже установлена."
fi

# Вспомогательные скрипты плеера
if [ ! -f "$HOME/.scripts/player_on.sh" ]; then
    cat << 'EOF' > "$HOME/.scripts/player_on.sh"
#!/bin/bash
xhost +local: >/dev/null 2>&1 || true
pgrep -x audacious >/dev/null || audacious -H --play &
command -v glava >/dev/null && { pgrep -x glava >/dev/null || glava --desktop & }
EOF
    chmod +x "$HOME/.scripts/player_on.sh"
fi

if [ ! -f "$HOME/.scripts/player_off.sh" ]; then
    cat << 'EOF' > "$HOME/.scripts/player_off.sh"
#!/bin/bash
audtool --playback-stop >/dev/null 2>&1 || true
pkill -x audacious || true
pkill -x glava || true
EOF
    chmod +x "$HOME/.scripts/player_off.sh"
fi

# 7. Настройка службы systemd и расширения GNOME
echo "[+] Шаг 7/7: Настройка службы systemd..."
cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Voice Assistant Service (Jarvis)
After=network.target sound.target pipewire.service graphical-session.target
[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python assistant.py
Restart=always
RestartSec=3
# === НАСТРОЙКИ ОКРУЖЕНИЯ ===
Environment=PYTHONUNBUFFERED=1
Environment=LANG=ru_RU.UTF-8
Environment=LC_ALL=ru_RU.UTF-8
# Графика GNOME
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_CURRENT_DESKTOP=GNOME
Environment=DESKTOP_SESSION=gnome
Environment=XDG_SESSION_TYPE=wayland
# Звук и DBus
Environment=XDG_RUNTIME_DIR=/run/user/$UID_NUM
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_NUM/bus
[Install]
WantedBy=default.target
EOF

EXT_UUID="jarvis-windows@voiceassistant"
EXT_SRC="$PROJECT_DIR/gnome/$EXT_UUID"
EXT_DST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
if [ -d "$EXT_SRC" ]; then
    mkdir -p "$HOME/.local/share/gnome-shell/extensions"
    ln -sfn "$EXT_SRC" "$EXT_DST"
    gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
fi

systemctl --user daemon-reload
systemctl --user enable voice-assistant.service

echo ""
echo "=================================================================="
echo "    ✅ Установка завершена успешно!                              "
echo "=================================================================="
echo "1. Ключи в $PROJECT_DIR/.env:"
echo "   GROQ_API_KEY — облачный диалог"
echo "   TINKOFF_TOKEN — биржа (сводка и сделки голосом)"
echo "   TINKOFF_AUTO_TRADE=true — фоновые заявки на живом счёте"
echo "   TINKOFF_SANDBOX=true — песочница без реальных денег"
echo "   CAMERA_INDEX — камера охраны (по умолчанию 0)"
echo "   Тумблер «Биржа и портфель»: настройки Джарвиса (правый клик по сфере)"
echo ""
echo "2. Управление службой ассистента:"
echo "   systemctl --user start voice-assistant.service    # Запуск"
echo "   systemctl --user restart voice-assistant.service  # Перезапуск"
echo "   systemctl --user status voice-assistant.service   # Статус"
echo "   journalctl --user -u voice-assistant.service -f   # Логи"
echo ""
echo "3. Консольный текстовый режим:"
echo "   $VENV_DIR/bin/python cli.py          # Интерактивный диалог в терминале"
echo "   $VENV_DIR/bin/python cli.py --mute   # Режим без звука"
echo ""
echo "4. Настройка аппаратного эхоподавления (AEC):"
echo "   ./setup_echo_cancel.sh"
echo "=================================================================="
