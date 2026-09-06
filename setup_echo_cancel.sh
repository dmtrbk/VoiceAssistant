#!/bin/bash
# Настройка системного эхоподавления PipeWire (WebRTC AEC) для Manjaro GNOME.
# Вычитает звук колонок/музыки из сигнала микрофона в реальном времени.

set -euo pipefail

echo "=== Настройка эхоподавления PipeWire (WebRTC Echo-Cancel) ==="

# 1. Проверяем наличие пакета webrtc-audio-processing
if ! pacman -Qs webrtc-audio-processing >/dev/null 2>&1; then
    echo "[+] Устанавливаю webrtc-audio-processing для PipeWire..."
    sudo pacman -S --needed --noconfirm webrtc-audio-processing
fi

# 2. Создаем директорию конфигурации PipeWire
CONFIG_DIR="$HOME/.config/pipewire/pipewire.conf.d"
mkdir -p "$CONFIG_DIR"

CONFIG_FILE="$CONFIG_DIR/99-echo-cancel.conf"

echo "[+] Создаю конфигурацию: $CONFIG_FILE"
cat << 'EOF' > "$CONFIG_FILE"
# Системное аппаратное эхоподавление WebRTC для PipeWire
# Автоматически фильтрует звук колонок (музыка, видео) из микрофона
context.modules = [
    {   name = libpipewire-module-echo-cancel
        args = {
            library.name  = "aec/libspa-aec-webrtc"
            node.latency = 1024/48000
            source.props = {
                node.name = "Echo-Cancel-Source"
                node.description = "Микрофон с эхоподавлением (AEC)"
            }
            sink.props = {
                node.name = "Echo-Cancel-Sink"
                node.description = "Выход с эхоподавлением (AEC)"
            }
        }
    }
]
EOF

# 3. Перезапускаем аудио-стек PipeWire пользователя
echo "[+] Перезапускаю службы PipeWire..."
systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true

echo ""
echo "============================================================"
echo "✅ Эхоподавление PipeWire успешно настроено!"
echo "Создан виртуальный источник 'Микрофон с эхоподавлением (AEC)'."
echo "Проверить статус аудио-устройств можно командой: wpctl status"
echo "============================================================"
