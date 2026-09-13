#!/bin/bash
# Настройка системного эхоподавления PipeWire (WebRTC AEC) для Manjaro GNOME.
# Вычитает звук текущего выхода (колонки или Bluetooth) из сигнала микрофона.

set -euo pipefail

echo "=== Настройка эхоподавления PipeWire (WebRTC Echo-Cancel) ==="

# 1. Проверяем наличие пакета webrtc-audio-processing
if ! pacman -Qs webrtc-audio-processing >/dev/null 2>&1; then
    echo "[+] Устанавливаю webrtc-audio-processing для PipeWire..."
    sudo pacman -S --needed --noconfirm webrtc-audio-processing
fi

# 2. Жёстко привязываем захват к аппаратному микрофону.
#    Иначе после смены default source на AEC модуль начинает слушать сам себя.
detect_hw_mic() {
    local current first
    current="$(pactl get-default-source 2>/dev/null || true)"
    if [[ "$current" == alsa_input.* ]]; then
        printf '%s\n' "$current"
        return 0
    fi
    first="$(pactl list short sources 2>/dev/null | awk '/alsa_input\./ {print $2; exit}')"
    if [[ -n "${first}" ]]; then
        printf '%s\n' "$first"
        return 0
    fi
    return 1
}

CAPTURE_TARGET=""
if CAPTURE_TARGET="$(detect_hw_mic)"; then
    echo "[+] Аппаратный микрофон для AEC: ${CAPTURE_TARGET}"
else
    echo "[!] Не нашёл alsa_input.* — AEC будет следовать за default source."
    echo "    После запуска ещё раз выполните скрипт, когда микрофон выбран в GNOME."
    CAPTURE_TARGET=""
fi

# 3. Создаём директорию конфигурации PipeWire
CONFIG_DIR="${HOME}/.config/pipewire/pipewire.conf.d"
mkdir -p "${CONFIG_DIR}"

CONFIG_FILE="${CONFIG_DIR}/99-echo-cancel.conf"

CAPTURE_TARGET_LINE=""
if [[ -n "${CAPTURE_TARGET}" ]]; then
    CAPTURE_TARGET_LINE="                target.object = \"${CAPTURE_TARGET}\""
fi

echo "[+] Создаю конфигурацию: ${CONFIG_FILE}"
cat > "${CONFIG_FILE}" << EOF
# Системное эхоподавление WebRTC для PipeWire.
# monitor.mode = true: опорный сигнал берётся с монитора текущего default sink
# (встроенные колонки, HDMI или Bluetooth — что выбрано сейчас).
# Виртуальный Echo-Cancel-Sink не создаём: приложения продолжают играть
# в обычный выход, AEC сам слушает, что туда идёт.
context.modules = [
    {   name = libpipewire-module-echo-cancel
        args = {
            library.name  = aec/libspa-aec-webrtc
            monitor.mode = true
            aec.args = {
                webrtc.extended_filter = false
                webrtc.noise_suppression = true
            }
            capture.props = {
                node.name = "Echo-Cancel-Capture"
                node.passive = true
${CAPTURE_TARGET_LINE}
            }
            source.props = {
                node.name = "Echo-Cancel-Source"
                node.description = "Микрофон с эхоподавлением (AEC)"
                node.virtual = false
                priority.session = 1200
            }
        }
    }
]
EOF

# 4. Перезапускаем аудио-стек пользователя
echo "[+] Перезапускаю службы PipeWire (звук на секунду пропадёт)..."
systemctl --user restart pipewire pipewire-pulse wireplumber

# 5. Ждём появления виртуального микрофона и делаем его источником по умолчанию
echo "[+] Жду появления Echo-Cancel-Source..."
AEC_READY=0
for _ in $(seq 1 25); do
    if pactl list short sources 2>/dev/null | grep -q 'Echo-Cancel-Source'; then
        AEC_READY=1
        break
    fi
    sleep 0.2
done

if [[ "${AEC_READY}" -eq 1 ]]; then
    # pactl set-default-source для виртуального AEC часто даёт "Not supported".
    # WirePlumber принимает wpctl set-default по id узла.
    AEC_ID="$(pw-dump | python3 -c '
import json, sys
for obj in json.load(sys.stdin):
    props = (obj.get("info") or {}).get("props") or {}
    if props.get("node.name") == "Echo-Cancel-Source" and props.get("media.class") == "Audio/Source":
        print(obj["id"])
        break
')"
    if [[ -z "${AEC_ID}" ]]; then
        echo "[!] Не нашёл id Echo-Cancel-Source"
        exit 1
    fi
    if ! wpctl set-default "${AEC_ID}"; then
        echo "[!] wpctl set-default ${AEC_ID} не сработал"
        exit 1
    fi
    echo "[+] Default source: Echo-Cancel-Source (id ${AEC_ID})"
else
    echo "[!] Echo-Cancel-Source не появился. Проверьте: wpctl status"
    echo "    журнал: journalctl --user -u pipewire -n 50 --no-pager"
    exit 1
fi

# 6. Не переводить Bluetooth в HSP/HFP (моно CVSD), когда ассистент открывает микрофон.
WP_DIR="${HOME}/.config/wireplumber/wireplumber.conf.d"
mkdir -p "${WP_DIR}"
cat > "${WP_DIR}/51-disable-bt-autoswitch.conf" << 'EOF'
wireplumber.settings = {
    bluetooth.autoswitch-to-headset-profile = false
}
EOF
if wpctl settings bluetooth.autoswitch-to-headset-profile false >/dev/null 2>&1; then
    echo "[+] Bluetooth autoswitch на гарнитуру выключен (A2DP не сбрасывается)"
fi

echo ""
echo "============================================================"
echo "Эхоподавление PipeWire настроено."
echo "Микрофон: «Микрофон с эхоподавлением (AEC)»."
echo "Опорный сигнал: текущий выход (колонки или Bluetooth)."
echo "Проверка: wpctl status"
echo "============================================================"
