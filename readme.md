# Голосовой ассистент (Manjaro GNOME)

Локальный голосовой ассистент для **Manjaro Linux** с сессией **GNOME**: распознавание **Vosk**, синтез **Piper TTS**, виджет-сфера на **PySide6**, плеер **Audacious**, локальный NLU и диалог через **Groq**.

Точка входа: `assistant.py` (микрофон + GUI + фоновый Telegram-слушатель). Команды из Telegram-бота обрабатывает тот же маршрутизатор, что и голос.

**Среда разработки и поддержки:** Manjaro Linux, GNOME (обычно Wayland). Пакеты — `pacman` или `pamac`. Звук — PipeWire (`wpctl`). Служба — `systemd --user`. Установщик (`setup.sh`) рассчитан на эту связку.

---

## Возможности

- Активация по имени, сессия внимания с тайм-аутом из `.env`
- Системная громкость (PipeWire / `wpctl`) отдельно от громкости плеера (`audtool`)
- Музыка и радио через Audacious; приглушение плеера на время сессии (ducking до 20%)
- Поиск в браузере, карты и маршруты (Яндекс по умолчанию)
- Охрана: веб-камера, снимки в Telegram
- Лампа Xiaomi / Yeelight
- Перезапуск службы `voice-assistant.service`
- Произвольные вопросы уходят в Groq, если узкий навык не сработал

---

## Структура

```
VoiceAssistant/
├── assistant.py              # Оркестратор: Vosk, Piper, сессия, GUI
├── telegram_listener.py      # Входящие команды Telegram Bot API
├── commands.py               # Маршрутизатор по навыкам
├── triggers.py               # Общие фразы: стоп / замолчи / спать / тишина
├── player_control.py         # Старт/стоп Audacious и Glava, авария «стоп»
├── browser.py                # Открытие URL (Chrome, иначе xdg-open)
├── volume_control.py         # Ducking громкости Audacious
├── indicator.py              # Сфера статусов на PySide6
├── nlu.py                    # TF-IDF + LogisticRegression
├── intents.json              # greeting, farewell, coin
├── setup.sh                  # Установка окружения и systemd
├── requirements.txt
├── .env.example
├── .env                      # Секреты, не в git
├── model/                    # Модель Vosk (не в git)
├── piper/                    # Бинарник Piper и .onnx (не в git)
└── skills/
    ├── __init__.py           # Регистрация и порядок навыков
    ├── restart.py
    ├── pentagon.py           # cmatrix («пентагон» / «матрица»)
    ├── security.py
    ├── datetime_skill.py
    ├── xiaomi_bulb.py
    ├── audacious.py
    ├── web_search.py
    ├── maps_search.py
    ├── telegram.py           # Приложение Telegram Desktop
    ├── system.py
    ├── local_nlu.py
    └── ai_chat.py            # Groq, fallback в конце цепочки
```

Каталога карточек навыков (SKILL_CARD) нет.

---

## Требования

**ОС:** Manjaro Linux, сессия GNOME. Виджет-сфера рассчитан на XWayland в этой сессии.

**Системные пакеты (ядро, `pacman` / `pamac`):**

- Python 3, `portaudio`, `alsa-utils`
- PipeWire: `pipewire`, `pipewire-pulse`, `wireplumber` (`wpctl`), `libpulse` (`paplay`, `pactl`)
- `audacious`, `audacious-plugins` (`audtool`)
- `gnome-terminal`, `nautilus`, `gnome-system-monitor`, `htop`, `neofetch`
- `cmatrix` (навык «пентагон»)
- `tk` (чёрная заставка охраны)
- `git`, `wget`, `unzip`, `libxcb`, `xorg-xhost`

**По желанию (не из официального репозитория или не обязательны):**

- Google Chrome (`google-chrome-stable`) — поиск и карты
- `telegram-desktop` — голосовое «открой телеграм»
- `glava` — визуализация; запускается из `~/.scripts/player_on.sh`, если скрипт есть

**Модели (кладём в проект, в git не входят):**

- Vosk: `model/` (например `vosk-model-small-ru-0.22`)
- Piper: `piper/piper` и `piper/models/ru_RU-dmitri-medium.onnx`

**Python:** зависимости из `requirements.txt` в `.venv`.

---

## Установка

### Автоматически

```bash
cd ~/VoiceAssistant
chmod +x setup.sh
./setup.sh
```

Скрипт ставит пакеты, создаёт `.venv`, ставит pip-зависимости, скачивает Vosk и Piper если их нет, пишет user-unit systemd, создаёт `.env` из `.env.example` **только если `.env` ещё нет**. Существующий `.env` не трогает.

Дальше заполните ключи в `.env` и запустите службу:

```bash
systemctl --user start voice-assistant.service
```

### Вручную

```bash
sudo pacman -Syu --needed \
  python python-pip portaudio alsa-utils git wget unzip tar \
  pipewire pipewire-pulse wireplumber libpulse \
  audacious audacious-plugins \
  cmatrix gnome-terminal nautilus gnome-system-monitor \
  htop neofetch libxcb tk xorg-xhost

cd ~/VoiceAssistant
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp -n .env.example .env
# отредактируйте .env — ключи не коммить
```

Модель Vosk (если папки `model/am` ещё нет):

```bash
mkdir -p model
wget https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
unzip vosk-model-small-ru-0.22.zip
mv vosk-model-small-ru-0.22/* model/
rm -rf vosk-model-small-ru-0.22 vosk-model-small-ru-0.22.zip
```

Piper (голос Dmitri Medium). Архив распаковывается во временную папку, содержимое копируется в `./piper` — так не затирается уже лежащая модель:

```bash
mkdir -p piper/models
tmpdir="$(mktemp -d)"
(
  cd "$tmpdir"
  wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_amd64.tar.gz
  tar -xf piper_amd64.tar.gz
  cp -a piper/. ~/VoiceAssistant/piper/
)
rm -rf "$tmpdir"
chmod +x piper/piper
wget -O piper/models/ru_RU-dmitri-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx
wget -O piper/models/ru_RU-dmitri-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json
```

`setup.sh` делает ту же загрузку и пропускает её, если бинарник и `.onnx` уже есть.

---

## Переменные `.env`

Имена и смысл. Значения-секреты в README и `.env.example` не пишутся.

| Переменная | Назначение | Если не задана |
|---|---|---|
| `GROQ_API_KEY` | Ключ Groq для диалога | Навык ИИ недоступен |
| `GROQ_MODEL` | Модель Groq | `groq/compound-mini` |
| `TELEGRAM_BOT_TOKEN` | Бот: команды и охрана | Слушатель не стартует |
| `TELEGRAM_CHAT_ID` | Разрешённый чат | Слушатель не стартует |
| `XIAOMI_BULB_IP` | IP лампы | Навык света попросит прописать IP |
| `XIAOMI_BULB_TOKEN` | Токен miio; без него — протокол Yeelight | Yeelight по IP |
| `ATTENTION_TIMEOUT` | Сколько секунд слушать после последней активности | **4** (в примере — **12**) |
| `SEARCH_PROVIDER` | Поиск по умолчанию: `yandex` / `google` | `yandex` |
| `MAPS_PROVIDER` | Карты: `yandex` / `google` | `yandex` |
| `AI_PROVIDER` | «открой нейросеть»: `yandex` (Алиса) / `gemini` | `yandex` |

`ATTENTION_TIMEOUT=12` — рекомендуемое значение в `.env.example`. Пока переменной нет, код берёт 4 секунды. Таймер не тикает, пока ассистент говорит или ждёт ответ Groq.

---

## systemd

User-unit для сессии GNOME. Имя: `voice-assistant.service`  
Файл: `~/.config/systemd/user/voice-assistant.service`

```ini
[Unit]
Description=Voice Assistant Service
After=network.target sound.target pipewire.service graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/real/VoiceAssistant
ExecStart=/home/real/VoiceAssistant/.venv/bin/python assistant.py
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
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

[Install]
WantedBy=default.target
```

`setup.sh` подставляет фактический каталог проекта и UID вместо `1000`.

```bash
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service
systemctl --user start voice-assistant.service
systemctl --user restart voice-assistant.service
systemctl --user status voice-assistant.service
journalctl --user -u voice-assistant.service -f
```

Перезапуск голосом: «перезагрузись», «перезапустись», «рестарт» — внутри вызывается тот же `systemctl --user restart voice-assistant.service`.

Виртуальное окружение — **`.venv`**, не `venv`.

---

## Плеер (опционально)

Если есть `~/.scripts/player_on.sh` и `player_off.sh`, ассистент запускает их через `systemd-run --user --scope`. Иначе при остановке: `audtool --playback-stop` и точечный `pkill -x` для `audacious` / `glava`. Существующие скрипты установщик не перезаписывает.

---

## Как пользоваться

**Имена активации:** «Джарвис», «Дарвис», «Сервис», «Жарис», «Умник».

После имени можно сразу сказать команду. Пока сессия жива, следующие фразы можно без имени. По тайм-ауту — снова idle, громкость плеера возвращается.

Быстрые команды (громкость, пауза, трек, «тишина») из сна **не удерживают** сессию.

### Сессия и речь (важно не путать)

| Фраза | Что происходит |
|---|---|
| **стоп** | Авария: гасит TTS, останавливает медиа и loopback, сессия в idle |
| **замолчи**, **подожди**, **хватит говорить** | Стоп TTS, сессия жива, приглушение музыки не снимается |
| **спать**, **отбой**, **все хватит** | Сессия в idle, громкость плеера назад, музыку не стопает |
| **тишина**, **выключи музыку** | Только плеер (навык Audacious). Это не «замолчи» |

Прощание NLU («пока», «до свидания», «выход») тоже уводит сессию в сон.

### Громкость

- **«громче» / «тише»** — системный sink, `wpctl` (+15% / −20%)
- **«громче музыку» / «тише музыку»** (или «плеер») — громкость Audacious, шаг 15%

Пока сессия активна, плеер приглушается до 20% и поднимается при уходе в idle. Команда громкости музыки из сна громкость не «восстанавливает» поверх вашего шага.

### Система

- «терминал», «файлы» / «проводник», «настройки»
- «htop», «неофетч», «системный монитор», «ютуб», «тик ток», «шахматы»
- «википедия …» / «что такое …»
- «выключи компьютер» — повтор в течение 20 секунд

### Музыка и радио

- «включи музыку» — папка `~/Музыка` или `~/Music`
- «пауза», «плей», «играй», «возобнови»
- «следующий» / «предыдущий» трек
- «что играет?»
- «включи радио» или станцию: Рекорд, Европа Плюс, Дорожное, Ретро, Наше, Панки Хой, Щас Спою, Вести, Маяк

### Поиск, карты, браузер

- «погугли …», «найди в яндексе …», «найди в интернете …»
- «открой браузер», «открой яндекс», «открой гугл»
- «открой алису» / «открой gemini» / «открой нейросеть»
- «где находится …», «найди на карте …»
- «как проехать до …», «как пройти до …» (авто / пешком / транспорт / велосипед)

### Охрана и свет

- «включи охрану» / «я ухожу» — минута на выход, затем камера и фото в Telegram
- «я пришел» / «отключи охрану» / «я дома»
- «включи свет», «выключи свет», «яркость на 50», «ярче», «тусклее»

### Прочее

- «который час», «какое сегодня число»
- «открой телеграм» / «закрой телеграм»
- «пентагон» / «матрица» — полноэкранный `cmatrix`
- «привет», «подбрось монетку» — локальный NLU
- «забудь все» / «очисти память» — сброс истории Groq
- Остальное — Groq (нужен `GROQ_API_KEY`)

Текст в разрешённый Telegram-чат обрабатывается теми же навыками.

---

## Отладка

| Симптом | Что проверить |
|---|---|
| Служба не стартует | `journalctl --user -u voice-assistant.service -f` |
| «Папка с моделью Vosk не найдена» | Каталог `model/` с `am/`, `graph/` |
| «Исполняемый файл Piper не найден» | `piper/piper` и `piper/models/ru_RU-dmitri-medium.onnx` |
| Нет ответа ИИ | `GROQ_API_KEY`, модель `GROQ_MODEL` |
| Нет команд из Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| «громче» не работает | `wpctl`, PipeWire в сессии пользователя |
| Музыка не приглушается | Audacious запущен, есть `audtool` |
| Сфера не видна | `DISPLAY`, user-session systemd, не root-ssh |
| Сессия слишком короткая | В `.env` задайте `ATTENTION_TIMEOUT=12` и перезапустите службу |

Перезапуск после правок `.env` или кода:

```bash
systemctl --user restart voice-assistant.service
```
