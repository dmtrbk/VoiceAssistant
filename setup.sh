#!/bin/bash
# Установка окружения голосового ассистента (Manjaro GNOME).
# Не перезаписывает существующий .env и не трогает requirements.txt.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/voice-assistant.service"
UID_NUM="$(id -u)"

echo "=== Настройка голосового ассистента (Manjaro GNOME) ==="
echo "[+] Каталог проекта: $PROJECT_DIR"

echo "[+] Системные пакеты (pacman)..."
CORE_PKGS=(
    python python-pip
    portaudio alsa-utils
    git wget unzip tar
    pipewire pipewire-pulse wireplumber libpulse
    audacious audacious-plugins
    cmatrix
    gnome-terminal nautilus gnome-system-monitor
    htop neofetch
    libxcb tk xorg-xhost
)
sudo pacman -Syu --needed --noconfirm "${CORE_PKGS[@]}"

for opt in telegram-desktop; do
    if pacman -Si "$opt" >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm "$opt" || echo "[!] Пакет $opt пропущен."
    fi
done

echo "[+] Каталоги..."
mkdir -p "$PROJECT_DIR/model"
mkdir -p "$PROJECT_DIR/piper/models"
mkdir -p "$HOME/.scripts"
mkdir -p "$SERVICE_DIR"

ENV_EXAMPLE="$PROJECT_DIR/.env.example"
if [ ! -f "$ENV_EXAMPLE" ]; then
    echo "[+] Создаю .env.example..."
    cat << 'EOF' > "$ENV_EXAMPLE"
# Groq — облачный диалог (обязательно для навыка ИИ, диалог в стиле Алисы)
GROQ_API_KEY=
GROQ_MODEL=groq/compound-mini

# Telegram-бот: входящие команды и уведомления охраны
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Голос синтеза Piper TTS: ru_RU-dmitri-medium.onnx (мужской голос Дмитрия), ru_RU-ruslan-medium.onnx, ru_RU-irina-medium.onnx
PIPER_MODEL=ru_RU-dmitri-medium.onnx
VOICE_SPEED=1.0

# Город по умолчанию для прогноза погоды
DEFAULT_CITY=Москва

# Лампа Xiaomi / Yeelight (опционально)
XIAOMI_BULB_IP=
XIAOMI_BULB_TOKEN=

# Сессия внимания, секунды. По умолчанию 12.
ATTENTION_TIMEOUT=12

# Поиск и карты: yandex или google
SEARCH_PROVIDER=yandex
MAPS_PROVIDER=yandex

# Браузерный ИИ по команде «открой нейросеть»: yandex (Алиса) или gemini
AI_PROVIDER=yandex
EOF
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[+] Копирую .env из .env.example (заполните ключи вручную)."
    cp "$ENV_EXAMPLE" "$PROJECT_DIR/.env"
else
    echo "[+] .env уже есть — не трогаю."
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "[+] Создаю виртуальное окружение .venv..."
    python -m venv "$VENV_DIR"
fi

echo "[+] Python-пакеты в .venv..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# --- Vosk ---
if [ ! -d "$PROJECT_DIR/model/am" ]; then
    echo "[+] Скачиваю модель Vosk (small-ru-0.22)..."
    tmpdir="$(mktemp -d)"
    (
        cd "$tmpdir"
        wget -q --show-progress \
            "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
        unzip -q vosk-model-small-ru-0.22.zip
        cp -a vosk-model-small-ru-0.22/. "$PROJECT_DIR/model/"
    )
    rm -rf "$tmpdir"
else
    echo "[+] Модель Vosk уже на месте — пропускаю загрузку."
fi

# --- Piper ---
if [ ! -x "$PROJECT_DIR/piper/piper" ]; then
    echo "[+] Скачиваю Piper TTS..."
    tmpdir="$(mktemp -d)"
    (
        cd "$tmpdir"
        wget -q --show-progress \
            "https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz"
        tar -xf piper_amd64.tar.gz
        # Архив даёт каталог piper/ с бинарником и .so
        cp -a piper/. "$PROJECT_DIR/piper/"
    )
    rm -rf "$tmpdir"
    chmod +x "$PROJECT_DIR/piper/piper"
else
    echo "[+] Бинарник Piper уже есть — пропускаю загрузку."
fi

PIPER_ONNX="$PROJECT_DIR/piper/models/ru_RU-dmitri-medium.onnx"
PIPER_JSON="$PIPER_ONNX.json"
if [ ! -s "$PIPER_ONNX" ]; then
    echo "[+] Скачиваю голос ru_RU-dmitri-medium..."
    wget -q --show-progress -O "$PIPER_ONNX" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx"
    wget -q --show-progress -O "$PIPER_JSON" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json"
else
    echo "[+] Голосовая модель Piper уже есть — пропускаю загрузку."
fi

# Скрипты плеера — только если пользователь ещё не завёл свои
if [ ! -f "$HOME/.scripts/player_on.sh" ]; then
    echo "[+] Создаю ~/.scripts/player_on.sh..."
    cat << 'EOF' > "$HOME/.scripts/player_on.sh"
#!/bin/bash
xhost +local: >/dev/null 2>&1 || true
pgrep -x audacious >/dev/null || audacious -H --play &
command -v glava >/dev/null && { pgrep -x glava >/dev/null || glava --desktop & }
EOF
    chmod +x "$HOME/.scripts/player_on.sh"
fi

if [ ! -f "$HOME/.scripts/player_off.sh" ]; then
    echo "[+] Создаю ~/.scripts/player_off.sh..."
    cat << 'EOF' > "$HOME/.scripts/player_off.sh"
#!/bin/bash
audtool --playback-stop >/dev/null 2>&1 || true
pkill -x audacious || true
pkill -x glava || true
EOF
    chmod +x "$HOME/.scripts/player_off.sh"
fi

echo "[+] Пишу $SERVICE_FILE ..."
cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Voice Assistant Service
After=network.target sound.target pipewire.service graphical-session.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python assistant.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=LANG=ru_RU.UTF-8
Environment=LC_ALL=ru_RU.UTF-8
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_CURRENT_DESKTOP=GNOME
Environment=DESKTOP_SESSION=gnome
Environment=XDG_SESSION_TYPE=wayland
Environment=XDG_RUNTIME_DIR=/run/user/$UID_NUM
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_NUM/bus

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable voice-assistant.service

echo ""
echo "=================================================="
echo "Готово (Manjaro GNOME)."
echo "Заполните ключи в $PROJECT_DIR/.env (если ещё не заполнены)."
echo ""
echo "  systemctl --user start voice-assistant.service"
echo "  systemctl --user restart voice-assistant.service"
echo "  systemctl --user status voice-assistant.service"
echo "  journalctl --user -u voice-assistant.service -f"
echo "=================================================="
