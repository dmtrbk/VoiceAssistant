🛠️ Инструкция по установке и настройке
1. Системные зависимости Manjaro Linux
Установите необходимые пакеты для работы с аудио, графикой и системными инструментами:

Bash
sudo pacman -Syu --needed base-devel python python-pip portaudio audacious audacious-plugins pipewire-pulse libxcb git unrar unzip xterm google-chrome
2. Настройка виртуального окружения Python
Перейдите в папку проекта:

Bash
cd ~/Voiceassistant
Создайте и активируйте виртуальное окружение:

Bash
python -m venv .venv
source .venv/bin/activate
Обновите pip и установите необходимые зависимости:

Bash
pip install --upgrade pip
pip install sounddevice vosk PySide6 scikit-learn groq python-dotenv requests opencv-python yeelight python-miio wikipedia-api
3. Установка моделей Vosk и Piper TTS
A. Модель распознавания речи Vosk
Bash
mkdir -p ~/Voiceassistant/model
wget [https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip](https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip)
unzip vosk-model-small-ru-0.22.zip
mv vosk-model-small-ru-0.22/* ~/Voiceassistant/model/
rm -rf vosk-model-small-ru-0.22 vosk-model-small-ru-0.22.zip
B. Синтезатор речи Piper (Голос «Dmitri Medium»)
Bash
mkdir -p ~/Voiceassistant/piper/models
wget [https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz](https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz)
tar -xf piper_amd64.tar.gz
mv piper/piper ~/Voiceassistant/piper/
chmod +x ~/Voiceassistant/piper/piper
rm -rf piper_amd64.tar.gz piper/

# Загрузка голосовой модели ru_RU-dmitri-medium
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx)
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx.json [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json)
4. Создание файла конфигурации (.env)
Создайте файл .env в корне проекта (~/Voiceassistant/.env):

Фрагмент кода
GROQ_API_KEY="ваш_api_ключ_groq"
GROQ_MODEL="llama-3.3-70b-versatile"

# Данные для Telegram Bot (управление + уведомления охраны)
TELEGRAM_BOT_TOKEN="ваш_токен_бота"
TELEGRAM_CHAT_ID="ваш_chat_id"

# Настройки смарт-лампы Xiaomi/Yeelight (опционально)
XIAOMI_BULB_IP="192.168.1.100"
XIAOMI_BULB_TOKEN="ваш_токен_лампы"
5. Скрипты управления плеером Audacious
Создайте директорию ~/.scripts и исполняемые скрипты:

Bash
mkdir -p ~/.scripts
Файл ~/.scripts/player_on.sh:

Bash
#!/bin/bash
pgrep audacious > /dev/null || audacious -d &
pgrep glava > /dev/null || glava &
Файл ~/.scripts/player_off.sh:

Bash
#!/bin/bash
audtool --playback-stop
pkill audacious
pkill glava
Сделайте скрипты исполняемыми:

Bash
chmod +x ~/.scripts/player_on.sh ~/.scripts/player_off.sh
6. Настройка автозапуска через Systemd (User Unit)
Создайте директорию для пользовательских служб:

Bash
mkdir -p ~/.config/systemd/user
Создайте файл ~/.config/systemd/user/voice-assistant.service:

Ini, TOML
[Unit]
Description=Voice Assistant Service (Dmitri / Wayland)
After=network.target sound.target pipewire.service graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/real/Voiceassistant
ExecStart=/home/real/Voiceassistant/.venv/bin/python assistant.py
Restart=always
RestartSec=3

# === НАСТРОЙКИ ===
Environment=PYTHONUNBUFFERED=1
Environment=LANG=ru_RU.UTF-8
Environment=LC_ALL=ru_RU.UTF-8

# Графика и рабочий стол
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_CURRENT_DESKTOP=GNOME
Environment=DESKTOP_SESSION=gnome
Environment=XDG_SESSION_TYPE=wayland

# Звук и сервисы
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

[Install]
WantedBy=default.target
Активируйте и запустите службу:

Bash
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service
systemctl --user start voice-assistant.service
🗣️ Список голосовых команд
Активационные слова (Wake Words): «Джарвис», «Дарвис», «Сервис», «Жарис», «Умник».

🔴 Экстренное управление
«Стоп» / «Замолчи» — Мгновенно прерывает озвучку Piper TTS, сбрасывает Vosk, останавливает Audacious и Glava, выгружает модуль loopback PipeWire.

💻 Система и громкость
«Терминал» / «Файлы» / «Настройки» — Запуск соответствующих системных приложений GNOME.

«Htop» / «Неофетч» / «Системный монитор» — Запуск/закрытие утилит мониторинга.

«Громче» / «Тише» — Управление системной громкостью через PipeWire (wpctl).

«Выключи компьютер» — Завершение работы Manjaro Linux.

🎵 Музыка и Радио
«Включи музыку» / «Пауза» / «Плей» / «Следующий трек» — Управление плеером Audacious.

«Что играет?» — Запрос текущей композиции через audtool.

«Включи радио [Название]» — Переключение на радиостанции (Рекорд, Европа Plus, Дорожное, Ретро, Наше, Вести, Маяк).

Автоматическое приглушение (Ducking): При подаче команд громкость плеера Audacious автоматически плавно снижается до 20% и восстанавливается после ответа.

🔍 Поиск и Справка
«Погугли [запрос]» / «Найди в Яндексе [запрос]» — Открытие поиска в Google Chrome.

«Где находится [место]» — Показ координат на Google Maps.

«Википедия [запрос]» — Чтение краткого фрагмента из Wikipedia.

🛡️ Охрана и Умный дом
«Включи охрану» / «Я ухожу» — Активация детекции движения через OpenCV. При обнаружении движения делается снимок и отправляется в Telegram.

«Я пришел» / «Отключи охрану» — Деактивация охранного режима.

«Включи свет» / «Яркость на [1-100]» — Управление лампой Xiaomi/Yeelight.

💬 ИИ Диалог и Telegram
Любой свободный запрос — Автоматически направляется в ИИ-модель Groq API (llama-3.3-70b-versatile).

Управление из Telegram — Все текстовые команды, отправленные Telegram-боту, обрабатываются аналогично голосовым командам с отправкой ответа обратно в чат.

🛠️ Команды для отладки
Проверить статус службы:

Bash
systemctl --user status voice-assistant.service
Просмотр логов в реальном времени:

Bash
journalctl --user -u voice-assistant.service -f
Перезапустить ассистента:

Bash
systemctl --user restart voice-assistant.service
"""

with open("/tmp/README.md", "w", encoding="utf-8") as f:
f.write(readme_md)

print("README.md file created successfully at /tmp/README.md")


```text?code_stdout&code_event_index=2
README.md file created successfully at /tmp/README.md

Подготовил документацию для голосового ассистента с учётом всех особенностей вашей системы (Asus N53SV, Manjaro GNOME, Wayland, .venv в ~/Voiceassistant, запуск через voice-assistant.service под управлением systemd, голос Dmitri Medium, Vosk, Qt6 UI-сфера и интеграция с Telegram/Groq API).

Готовый файл Markdown сохранён:
[file-tag: code-generated-file-tmp_README.md]

Также сформирована печатная версия документации в формате PDF:
[file-tag: code-generated-file-tmp_README.pdf]

Текст и структура файла README.md
Markdown
# 🎙️ Voice Assistant «Jarvis» (Manjaro GNOME / Wayland)

Интерактивный голосовой ассистент с анимированным виджетом-сферой на рабочем столе PySide6 (Qt6), работающий в среде **Manjaro GNOME** (Wayland через XWayland/xcb). Проект основан на локальном распознавании речи (**Vosk**), нейросетевом синтезе речи (**Piper TTS** с моделью **Dmitri Medium**), фоновом управлении плеером **Audacious** и подключении к облачным/локальным NLU и ИИ-моделям (**Groq API**).

---

## 📂 Структура проекта

Базовый путь размещения проекта в домашней директории пользователя: `~/Voiceassistant` (или `/home/real/Voiceassistant`).

```text
Voiceassistant/
├── .env                         # Файл переменных окружения (API-ключи, токены Telegram)
├── .venv/                       # Виртуальное окружение Python
├── assistant.py                 # Главный оркестратор (Vosk + Piper + GUI + Telegram)
├── commands.py                  # Маршрутизатор команд по навыкам
├── indicator.py                 # Анимированный виджет-сфера на PySide6 (Qt6)
├── intents.json                 # Шаблоны фраз и ответов для NLU
├── nlu.py                       # Локальный NLU-классификатор (scikit-learn: TF-IDF + LogisticRegression)
├── volume_control.py            # Модуль приглушения и автовосстановления громкости Audacious (Ducking)
├── telegram_listener.py         # Фоновый слушатель Telegram Bot API
├── requirements.txt             # Зависимости Python
├── model/                       # Локальная языковая модель Vosk (создается вручную)
│   ├── am/
│   ├── graph/
│   └── ... (файлы модели)
├── piper/                       # Движок локального синтеза Piper TTS (создается вручную)
│   ├── piper                    # Бинарный исполняемый файл piper
│   └── models/                  # Голосовая модель Piper
│       ├── ru_RU-dmitri-medium.onnx
│       └── ru_RU-dmitri-medium.onnx.json
└── skills/                      # Встроенные навыки ассистента
    ├── __init__.py              # Регистрация и приоритезация навыков
    ├── base.py                  # Базовые классы RequestContext и BaseSkill
    ├── ai_chat.py               # Общение с ИИ (Groq API) + оффлайн-музыка при сбое сети
    ├── audacious.py             # Управление плеером Audacious и радиостанциями
    ├── datetime_skill.py        # Время и дата
    ├── local_nlu.py             # Локальные триггеры NLU (приветствия, монетка, выход)
    ├── maps_search.py           # Гео-поиск в Google Картах
    ├── security.py              # Режим охраны, веб-камера (OpenCV), отправка кадров в Telegram
    ├── system.py                # Системные приложения (htop, neofetch, громкость, выключение ПК)
    ├── telegram.py              # Запуск и закрытие Telegram
    ├── utils.py                 # Вспомогательные функции (Telegram Bot API)
    ├── web_search.py            # Поиск в браузере (Google, Яндекс) и Gemini AI
    └── xiaomi_bulb.py           # Управление смарт-лампой Yeelight/Xiaomi
🛠️ Инструкция по установке и настройке
1. Системные зависимости Manjaro Linux
Установите необходимые пакеты для работы с аудио, графикой и системными инструментами:

Bash
sudo pacman -Syu --needed base-devel python python-pip portaudio audacious audacious-plugins pipewire-pulse libxcb git unrar unzip xterm google-chrome
2. Настройка виртуального окружения Python
Перейдите в папку проекта:

Bash
cd ~/Voiceassistant
Создайте и активируйте виртуальное окружение:

Bash
python -m venv .venv
source .venv/bin/activate
Обновите pip и установите необходимые зависимости:

Bash
pip install --upgrade pip
pip install sounddevice vosk PySide6 scikit-learn groq python-dotenv requests opencv-python yeelight python-miio wikipedia-api
3. Установка моделей Vosk и Piper TTS
A. Модель распознавания речи Vosk
Bash
mkdir -p ~/Voiceassistant/model
wget [https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip](https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip)
unzip vosk-model-small-ru-0.22.zip
mv vosk-model-small-ru-0.22/* ~/Voiceassistant/model/
rm -rf vosk-model-small-ru-0.22 vosk-model-small-ru-0.22.zip
B. Синтезатор речи Piper (Голос «Dmitri Medium»)
Bash
mkdir -p ~/Voiceassistant/piper/models
wget [https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz](https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz)
tar -xf piper_amd64.tar.gz
mv piper/piper ~/Voiceassistant/piper/
chmod +x ~/Voiceassistant/piper/piper
rm -rf piper_amd64.tar.gz piper/

# Загрузка голосовой модели ru_RU-dmitri-medium
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx)
wget -O ~/Voiceassistant/piper/models/ru_RU-dmitri-medium.onnx.json [https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json](https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json)
4. Создание файла конфигурации (.env)
Создайте файл .env в корне проекта (~/Voiceassistant/.env):

Фрагмент кода
GROQ_API_KEY="ваш_api_ключ_groq"
GROQ_MODEL="llama-3.3-70b-versatile"

# Данные для Telegram Bot (управление + уведомления охраны)
TELEGRAM_BOT_TOKEN="ваш_токен_бота"
TELEGRAM_CHAT_ID="ваш_chat_id"

# Настройки смарт-лампы Xiaomi/Yeelight (опционально)
XIAOMI_BULB_IP="192.168.1.100"
XIAOMI_BULB_TOKEN="ваш_токен_лампы"
5. Скрипты управления плеером Audacious
Создайте директорию ~/.scripts и исполняемые скрипты:

Bash
mkdir -p ~/.scripts
Файл ~/.scripts/player_on.sh:

Bash
#!/bin/bash
pgrep audacious > /dev/null || audacious -d &
pgrep glava > /dev/null || glava &
Файл ~/.scripts/player_off.sh:

Bash
#!/bin/bash
audtool --playback-stop
pkill audacious
pkill glava
Сделайте скрипты исполняемыми:

Bash
chmod +x ~/.scripts/player_on.sh ~/.scripts/player_off.sh
6. Настройка автозапуска через Systemd (User Unit)
Создайте директорию для пользовательских служб:

Bash
mkdir -p ~/.config/systemd/user
Создайте файл ~/.config/systemd/user/voice-assistant.service:

Ini, TOML
[Unit]
Description=Voice Assistant Service (Dmitri / Wayland)
After=network.target sound.target pipewire.service graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/real/Voiceassistant
ExecStart=/home/real/Voiceassistant/.venv/bin/python assistant.py
Restart=always
RestartSec=3

# === НАСТРОЙКИ ===
Environment=PYTHONUNBUFFERED=1
Environment=LANG=ru_RU.UTF-8
Environment=LC_ALL=ru_RU.UTF-8

# Графика и рабочий стол
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_CURRENT_DESKTOP=GNOME
Environment=DESKTOP_SESSION=gnome
Environment=XDG_SESSION_TYPE=wayland

# Звук и сервисы
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

[Install]
WantedBy=default.target
Активируйте и запустите службу:

Bash
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service
systemctl --user start voice-assistant.service
🗣️ Список голосовых команд
Активационные слова (Wake Words): «Джарвис», «Дарвис», «Сервис», «Жарис», «Умник».

🔴 Экстренное управление
«Стоп» / «Замолчи» — Мгновенно прерывает озвучку Piper TTS, сбрасывает Vosk, останавливает Audacious и Glava, выгружает модуль loopback PipeWire.

💻 Система и громкость
«Терминал» / «Файлы» / «Настройки» — Запуск соответствующих системных приложений GNOME.

«Htop» / «Неофетч» / «Системный монитор» — Запуск/закрытие утилит мониторинга.

«Громче» / «Тише» — Управление системной громкостью через PipeWire (wpctl).

«Выключи компьютер» — Завершение работы Manjaro Linux.

🎵 Музыка и Радио
«Включи музыку» / «Пауза» / «Плей» / «Следующий трек» — Управление плеером Audacious.

«Что играет?» — Запрос текущей композиции через audtool.

«Включи радио [Название]» — Переключение на радиостанции (Рекорд, Европа Plus, Дорожное, Ретро, Наше, Вести, Маяк).

Автоматическое приглушение (Ducking): При подаче команд громкость плеера Audacious автоматически плавно снижается до 20% и восстанавливается после ответа.

🔍 Поиск и Справка
«Погугли [запрос]» / «Найди в Яндексе [запрос]» — Открытие поиска в Google Chrome.

«Где находится [место]» — Показ координат на Google Maps.

«Википедия [запрос]» — Чтение краткого фрагмента из Wikipedia.

🛡️ Охрана и Умный дом
«Включи охрану» / «Я ухожу» — Активация детекции движения через OpenCV. При обнаружении движения делается снимок и отправляется в Telegram.

«Я пришел» / «Отключи охрану» — Деактивация охранного режима.

«Включи свет» / «Яркость на [1-100]» — Управление лампой Xiaomi/Yeelight.

💬 ИИ Диалог и Telegram
Любой свободный запрос — Автоматически направляется в ИИ-модель Groq API (llama-3.3-70b-versatile).

Управление из Telegram — Все текстовые команды, отправленные Telegram-боту, обрабатываются аналогично голосовым командам с отправкой ответа обратно в чат.

🛠️ Команды для отладки
Проверить статус службы:

Bash
systemctl --user status voice-assistant.service
Просмотр логов в реальном времени:

Bash
journalctl --user -u voice-assistant.service -f
Перезапустить ассистента:

Bash
systemctl --user restart voice-assistant.service




## Установка и настройка в Manjaro XFCE

### 1. Установка системных зависимостей

Перед запуском убедитесь, что в системе установлены все необходимым пакеты (Python, PortAudio для работы с микрофоном и утилиты сборки):

```bash
sudo pacman -Syu --needed python python-pip portaudio alsa-utils git
Если используются AUR-пакеты (например, для специфических голосов TTS или библиотек):Bashpamac build <имя_пакета>
2. Клонирование и настройка окруженияКлонируйте репозиторий:Bashgit clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ>
cd <ИМЯ_ПАПКИ_ПРОЕКТА>
Создайте и активируйте виртуальное окружение Python:Bashpython -m venv venv
source venv/bin/activate
Установите зависимостями из requirements.txt:Bashpip install -r requirements.txt
3. Настройка автозапуска в Manjaro XFCEВ XFCE настроить автозапуск проекта можно двумя способами:Способ A: Через графический интерфейс XFCE (Рекомендуется)Откройте Меню приложений $\rightarrow$ Настройки $\rightarrow$ Сеансы и автозапуск (Session and Startup).Перейдите во вкладку Автозапуск приложений (Application Autostart).Нажмите кнопку Добавить (+) внизу.Заполните поля:Имя: Voice Assistant (или любое другое)Описание: Голосовой ассистентКоманда:Bash/bin/bash -c "source /path/to/project/venv/bin/activate && python /path/to/project/ai_chat.py"
(Замените /path/to/project/ на реальный абсолютный путь к вашей папке).Способ B: Через systemd-service (пользовательский сервис)Создайте файл пользовательского сервиса:Bashmkdir -p ~/.config/systemd/user/
nano ~/.config/systemd/user/voice-assistant.service
Вставьте следующую конфигурацию:Ini, TOML[Unit]
Description=Voice Assistant Service
After=network.target sound.target

[Service]
Type=simple
WorkingDirectory=/path/to/project/
ExecStart=/path/to/project/venv/bin/python /path/to/project/ai_chat.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
Перезагрузите демоны systemd и включите автозапуск:Bashsystemctl --user daemon-reload
systemctl --user enable voice-assistant.service --now
Проверить статус службы:Bashsystemctl --user status voice-assistant.service



## Установка и настройка в Manjaro KDE Plasma

### 1. Установка системных зависимостей

Перед запуском убедитесь, что в системе установлены необходимые пакеты (Python, PortAudio для работы со звуком и микрофоном, утилиты сборки):

```bash
sudo pacman -Syu --needed python python-pip portaudio alsa-utils git
Если вы используете пакеты из AUR:Bashpamac build <имя_пакета>
2. Клонирование и настройка окруженияКлонируйте репозиторий:Bashgit clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ>
cd <ИМЯ_ПАПКИ_ПРОЕКТА>
Создайте и активируйте виртуальное окружение Python:Bashpython -m venv venv
source venv/bin/activate
Установите зависимости из requirements.txt:Bashpip install -r requirements.txt
3. Настройка автозапуска в Manjaro KDE PlasmaВ KDE Plasma настроить автозапуск можно через Параметры системы или через systemd.Способ A: Через Параметры системы KDE (GUI)Откройте Параметры системы (System Settings) $\rightarrow$ Параметры окружения $\rightarrow$ Автозапуск (Autostart).Внизу нажмите Добавить... (Add...) и выберите Добавить скрипт... (Add Application / Add Command).В поле команды укажите:Bash/bin/bash -c "source /path/to/project/venv/bin/activate && python /path/to/project/ai_chat.py"
(Укажите реальный полный путь к директории вашего проекта).Сохраните изменения.Способ B: Через systemd-service (Пользовательская служба)Создайте каталог для пользовательских сервисов и файл службы:Bashmkdir -p ~/.config/systemd/user/
nano ~/.config/systemd/user/voice-assistant.service
Вставьте следующую конфигурацию:Ini, TOML[Unit]
Description=Voice Assistant Service
After=network.target sound.target

[Service]
Type=simple
WorkingDirectory=/path/to/project/
ExecStart=/path/to/project/venv/bin/python /path/to/project/ai_chat.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
Обновите конфигурацию systemd и включите автозапуск:Bashsystemctl --user daemon-reload
systemctl --user enable voice-assistant.service --now
Проверить статус службы и просмотреть логи:Bashsystemctl --user status voice-assistant.service
journalctl --user -u voice-assistant.service -f




### Установка зависимостей

1. Активируйте виртуальное окружение:
   ```bash
   source venv/bin/activate
Установите все необходимые библиотеки Python из файла requirements.txt:

Bash
pip install -r requirements.txt


## Быстрая автоматическая установка (Manjaro XFCE / KDE)



Bash
chmod +x setup.sh
./setup.sh
Укажите ваш API-ключ в созданном файле .env:

Bash
nano .env
Запустите службу:

Bash
systemctl --user start voice-assistant.service
