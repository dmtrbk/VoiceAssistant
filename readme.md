# 🎙️ Voice Assistant «Jarvis» (Manjaro Linux)

Интерактивный голосовой ассистент с анимированным виджетом-сферой на **PySide6 (Qt6)** для **Manjaro Linux (GNOME / XFCE / KDE Plasma)**.

Проект совмещает локальное распознавание речи (**Vosk**), нейросетевой синтез (**Piper TTS**), управление звуковым окружением/плеером (**Audacious + PipeWire**), локальный NLU-классификатор и интеграцию с ИИ-моделью **Groq API**.

---

## 📂 Структура проекта

Voiceassistant/
├── .env                         # API-ключи, токены Telegram и настройки устройств
├── .venv/                       # Виртуальное окружение Python
├── assistant.py                 # Главный оркестратор (Vosk + Piper + GUI + Telegram)
├── commands.py                  # Маршрутизатор команд по навыкам
├── indicator.py                 # Анимированный виджет-сфера на PySide6 (Qt6)
├── intents.json                 # Шаблоны фраз и ответов для NLU
├── nlu.py                       # Локальный NLU-классификатор (TF-IDF + LogisticRegression)
├── volume_control.py            # Модуль приглушения/восстановления громкости (Ducking)
├── telegram_listener.py         # Фоновый слушатель Telegram Bot API
├── requirements.txt             # Зависимости Python
├── model/                       # Локальная модель распознавания Vosk
├── piper/                       # Движок Piper TTS и голосовая модель (.onnx)
└── skills/                      # Встроенные навыки ассистента
    ├── __init__.py              # Регистрация и приоритезация навыков
    ├── base.py                  # Базовые классы RequestContext и BaseSkill
    ├── ai_chat.py               # Общение с ИИ (Groq API) + оффлайн-музыка при сбое
    ├── audacious.py             # Управление плеером Audacious и радиостанциями
    ├── datetime_skill.py        # Время и дата
    ├── local_nlu.py             # Локальные триггеры NLU (приветствия, монетка, выход)
    ├── maps_search.py           # Гео-поиск в Google Картах
    ├── security.py              # Режим охраны, веб-камера (OpenCV), фото в Telegram
    ├── system.py                # Системные приложения, громкость, выключение ПК
    ├── telegram.py              # Управление приложением Telegram
    ├── restart.py               # Самоперезапуск службы через systemd
    ├── web_search.py            # Поиск в браузере (Google, Яндекс)
    └── xiaomi_bulb.py           # Управление смарт-лампой Yeelight/Xiaomi

---

## ⚡ Быстрая установка (Автоматическая)

Если вы используете `setup.sh`, подготовка окружения и службы выполняется в три команды:

chmod +x setup.sh
./setup.sh
systemctl --user start voice-assistant.service

---

## 🛠️ Пошаговая ручная установка и настройка

### 1. Системные зависимости Manjaro Linux
Установите пакеты для работы со звуком, графикой и системными инструментами:

sudo pacman -Syu --needed base-devel python python-pip portaudio alsa-utils audacious audacious-plugins pipewire-pulse libxcb git unrar unzip xterm google-chrome

### 2. Настройка виртуального окружения Python
cd ~/Voiceassistant
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

### 3. Установка моделей Vosk и Piper TTS

A. Модель распознавания речи Vosk
mkdir -p ~/Voiceassistant/model
wget [https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip](https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip)
unzip vosk-model-small-ru-0.22.zip
mv vosk-model-small-ru-0.22/* ~/Voiceassistant/model/
rm -rf vosk-model-small-ru-0.22 vosk-model-small-ru-0.22.zip

B. Синтезатор речи Piper (Голос «Dmitri Medium»)
mkdir -p ~/Voiceassistant/piper/models
wget [https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz](https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz)
tar -xf piper_amd64.tar.gz
mv piper/piper ~/Voiceassistant/piper/
chmod +x ~/Voiceassistant/piper/piper
rm -rf piper_amd64.tar.gz piper/

# Загрузка голосовой модели ru_RU-dmitri-medium
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx)
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx.json [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json)

### 4. Создание файла конфигурации (.env)
Создайте файл `.env` в корне проекта (`~/Voiceassistant/.env`):

GROQ_API_KEY="ваш_api_ключ_groq"
GROQ_MODEL="llama-3.3-70b-versatile"

# Данные для Telegram Bot (управление + уведомления охраны)
TELEGRAM_BOT_TOKEN="ваш_токен_бота"
TELEGRAM_CHAT_ID="ваш_chat_id"

# Настройки смарт-лампы Xiaomi/Yeelight (опционально)
XIAOMI_BULB_IP="192.168.1.100"
XIAOMI_BULB_TOKEN="ваш_токен_лампы"

### 5. Скрипты управления плеером Audacious
Создайте директорию `~/.scripts` и скрипты управления:

mkdir -p ~/.scripts

~/.scripts/player_on.sh:
#!/bin/bash
pgrep audacious > /dev/null || audacious -d &
pgrep glava > /dev/null || glava &

~/.scripts/player_off.sh:
#!/bin/bash
audtool --playback-stop
pkill audacious
pkill glava

Сделайте их исполняемыми:
chmod +x ~/.scripts/player_on.sh ~/.scripts/player_off.sh

### 6. Автозапуск (Systemd User Unit)
Создайте файл `~/.config/systemd/user/voice-assistant.service`:

[Unit]
Description=Voice Assistant Service (Jarvis)
After=network.target sound.target pipewire.service graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/real/Voiceassistant
ExecStart=/home/real/Voiceassistant/.venv/bin/python assistant.py
Restart=always
RestartSec=3

# Среда выполнения
Environment=PYTHONUNBUFFERED=1
Environment=LANG=ru_RU.UTF-8
Environment=LC_ALL=ru_RU.UTF-8

# Графика (X11 / Wayland)
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_CURRENT_DESKTOP=GNOME
Environment=DESKTOP_SESSION=gnome
Environment=XDG_SESSION_TYPE=wayland

# Сессионная шина и звук
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

[Install]
WantedBy=default.target

Активируйте и запустите службу:
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service --now

> **Альтернатива (XFCE / KDE GUI Autostart):**
> Если вы предпочитаете автозапуск через графические настройки рабочей среды, добавьте команду:
> `/bin/bash -c "source ~/Voiceassistant/.venv/bin/activate && python ~/Voiceassistant/assistant.py"`

---

## 🗣️ Голосовые команды

**Активация (Wake Words):** «Джарвис», «Дарвис», «Сервис», «Жарис», «Умник».

* 🔴 **Экстренное управление:**  
  * `«Стоп»` / `«Замолчи»` — Мгновенно прерывает озвучку, сбрасывает Vosk, останавливает Audacious и Glava.
  * `«Перезагрузись»` — Выполняет мягкий перезапуск службы через systemd.
* 💻 **Система и громкость:**  
  * `«Терминал»` / `«Файлы»` / `«Настройки»` — Запуск системных приложений.
  * `«Htop»` / `«Неофетч»` / `«Системный монитор»` — Утилиты мониторинга.
  * `«Громче»` / `«Тише»` — Управление громкостью через PipeWire (`wpctl`).
  * `«Выключи компьютер»` — Завершение работы Manjaro Linux.
* 🎵 **Музыка и Радио:**  
  * `«Включи музыку»` / `«Пауза»` / `«Плей»` / `«Следующий трек»` — Управление Audacious.
  * `«Что играет?»` — Название текущей композиции.
  * `«Включи радио [Название]»` — Переключение радиостанций (Рекорд, Европа Plus, Дорожное, Ретро, Наше, Вести, Маяк).
  *(При подаче команд громкость плеера плавно приглушается до 20% и восстанавливается после ответа)*.
* 🔍 **Поиск и Справка:**  
  * `«Погугли [запрос]»` / `«Найди в Яндексе [запрос]»` — Открытие результатов в браузере.
  * `«Где находится [место]»` — Google Карта.
  * `«Википедия [запрос]»` — Чтение краткого фрагмента из Wikipedia.
* 🛡️ **Охрана и Умный дом:**  
  * `«Включи охрану»` / `«Я ухожу»` — Детекция движения OpenCV + отправка фото в Telegram.
  * `«Я пришел»` / `«Отключи охрану»` — Выключение охранного режима.
  * `«Включи свет»` / `«Яркость на [1-100]»` — Управление лампой Xiaomi/Yeelight.
* 💬 **ИИ Диалог и Telegram:**  
  * Произвольный вопрос автоматически адресуется в ИИ-модель **Groq API** (`llama-3.3-70b-versatile`).
  * Команды, отправленные в Telegram-бот, обрабатываются ассистентом аналогично голосовым.

---

## 🛠️ Полезные команды отладки

# Проверить статус службы
systemctl --user status voice-assistant.service

# Просмотр логов в реальном времени
journalctl --user -u voice-assistant.service -f

# Перезапустить ассистента вручную
systemctl --user restart voice-assistant.service
