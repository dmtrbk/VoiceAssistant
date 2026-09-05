# Голосовой ассистент «Джарвис» (Manjaro GNOME)

Локальный голосовой ассистент **Джарвис** для **Manjaro Linux** с сессией **GNOME**: оффлайн-распознавание речи **Vosk**, качественный мужской синтез речи **Piper TTS (Dmitri)**, диалоговые способности в живом и сообразительном стиле Алисы, интерактивный виджет-сфера на **PySide6**, плеер **Audacious**, локальный NLU и облачный диалоговый интеллект через **Groq**.

Точка входа: `assistant.py` (микрофон + GUI + фоновый Telegram-слушатель). Команды из Telegram-бота обрабатывает тот же маршрутизатор, что и голос.

**Среда разработки и поддержки:** Manjaro Linux, GNOME (Wayland / XWayland). Пакеты — `pacman` или `pamac`. Звук — PipeWire (`wpctl`). Служба — `systemd --user`. Установщик (`setup.sh`) рассчитан на эту связку.

---

## Возможности

- **Личность и голос:** Мужской голос Piper (`ru_RU-dmitri-medium.onnx`), активация по имени «Джарвис» или «Умник», общение в мужском роде («рад», «понял», «сделал», «готов»), живой диалог в стиле Алисы (остроумие, сообразительность, лёгкая ирония).
- **Сессия внимания (Attention Timeout):** После обращения ассистент слушает последующие команды без повторения имени (тайм-аут из `.env`, по умолчанию 12 с).
- **Barge-in (Мгновенное перебивание):** Возможность прервать длинную речь ассистента именем или командами «стоп» / «замолчи» / «спать».
- **Погода (Open-Meteo):** Точный прогноз на сегодня и завтра с деталями об осадках, зонте, ветре и температуре для любого города без обязательного API-ключа.
- **Таймеры:** Фоновый отсчет времени («поставь таймер на 5 минут», «сколько осталось») с голосовым оповещением по окончании.
- **Быстрый калькулятор:** Мгновенный расчет математических выражений, корней, степеней, процентов и словесных чисел без задержки на LLM.
- **Развлечения:** Анекдоты, интересные факты, тосты, сказки, мудрые цитаты и комплименты.
- **Музыка, радио и звуки природы:** Радиостанции (Рекорд, Европа Плюс, Дорожное, Наше и др.), звуки природы (шум дождя, лес, море, костер), локальная музыка из `~/Музыка`; автоматическое приглушение плеера (ducking до 20%) во время диалога.
- **Поиск и карты:** Яндекс / Google Поиск, Яндекс / Google Карты и автоматическое построение маршрутов.
- **Охрана:** Веб-камера, детекция движения через OpenCV, снимки тревоги в Telegram.
- **Умный дом:** Лампы Xiaomi / Yeelight (включение, выключение, яркость прописью и цифрами).
- **Системное управление:** Регулировка громкости PipeWire (`wpctl`), запуск/закрытие утилит (`htop`, `neofetch`, `gnome-system-monitor`), статьи Википедии.
- **Диалог с памятью (Groq):** Свободное общение на любые темы с удержанием контекста и историей беседы.

---

## Структура

```
VoiceAssistant/
├── assistant.py              # Оркестратор: Vosk, Piper (Dmitri), сессия, GUI
├── telegram_listener.py      # Входящие команды Telegram Bot API
├── commands.py               # Маршрутизатор по цепочке навыков
├── triggers.py               # Общие триггеры: стоп / замолчи / спать / тишина
├── player_control.py         # Старт/стоп Audacious и Glava, авария «стоп»
├── browser.py                # Открытие URL (Chrome, иначе xdg-open)
├── volume_control.py         # Ducking громкости Audacious (кэш на диске)
├── indicator.py              # Интерактивная сфера статусов на PySide6
├── nlu.py                    # TF-IDF + LogisticRegression на n-граммах
├── intents.json              # Базовые интенты (приветствия, прощания, монетка, личность)
├── setup.sh                  # Скрипт автоустановки окружения и systemd
├── requirements.txt          # Python-зависимости
├── .env.example              # Пример переменных окружения
├── .env                      # Конфигурация и API-ключи (не в git)
├── model/                    # Модель Vosk (не в git)
├── piper/                    # Бинарник Piper и .onnx модели (не в git)
└── skills/
    ├── __init__.py           # Регистрация и порядок навыков
    ├── weather.py            # Погода и прогноз (Open-Meteo)
    ├── timer.py              # Таймеры и будильники
    ├── calculator.py         # Быстрый калькулятор и математика
    ├── jokes_facts.py        # Анекдоты, факты, тосты, сказки, комплименты
    ├── datetime_skill.py     # Дата, время, день недели
    ├── audacious.py          # Музыка, радио и звуки природы
    ├── web_search.py         # Поиск в интернете (Яндекс / Google)
    ├── maps_search.py        # Карты и маршруты (Яндекс / Google Карты)
    ├── xiaomi_bulb.py        # Умная лампа Xiaomi / Yeelight
    ├── security.py           # Видеонаблюдение и тревожные снимки
    ├── system.py             # Системные команды и утилиты
    ├── restart.py            # Перезапуск службы
    ├── pentagon.py           # Анимация cmatrix («пентагон» / «матрица»)
    ├── telegram.py           # Запуск Telegram Desktop
    ├── local_nlu.py          # Локальные быстрые ответы
    └── ai_chat.py            # Groq (диалог в стиле Алисы, персона Джарвис)
```

---

## Требования

**ОС:** Manjaro Linux, сессия GNOME (Wayland / XWayland).

**Системные пакеты (`pacman` / `pamac`):**

- Python 3, `portaudio`, `alsa-utils`
- PipeWire: `pipewire`, `pipewire-pulse`, `wireplumber` (`wpctl`), `libpulse` (`paplay`, `pactl`)
- `audacious`, `audacious-plugins` (`audtool`)
- `gnome-terminal`, `nautilus`, `gnome-system-monitor`, `htop`, `neofetch`
- `cmatrix` (навык «пентагон»)
- `tk` (заставка режима охраны)
- `git`, `wget`, `unzip`, `tar`, `libxcb`, `xorg-xhost`

**Модели (скачиваются автоматически в `setup.sh`):**

- Vosk: `model/` (`vosk-model-small-ru-0.22`)
- Piper TTS: `piper/piper` и `piper/models/ru_RU-dmitri-medium.onnx` (мужской голос Дмитрия)

---

## Установка

### Автоматически (рекомендуется)

```bash
cd ~/VoiceAssistant
chmod +x setup.sh
./setup.sh
```

Скрипт установит системные пакеты, создаст `.venv`, установит pip-зависимости, скачает модели Vosk и Piper Dmitri, настроит user-unit systemd и создаст файл `.env`.

После установки заполните ключи в `.env` и запустите службу:

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
# Отредактируйте .env
```

Загрузка моделей:
```bash
# Модель Vosk
mkdir -p model
wget https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
unzip vosk-model-small-ru-0.22.zip
cp -a vosk-model-small-ru-0.22/. model/
rm -rf vosk-model-small-ru-0.22 vosk-model-small-ru-0.22.zip

# Piper TTS (голос Dmitri)
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

---

## Переменные `.env`

| Переменная | Назначение | Значение по умолчанию |
|---|---|---|
| `GROQ_API_KEY` | API-ключ Groq для диалогового ИИ | `—` (модуль ИИ отключен) |
| `GROQ_MODEL` | Модель Groq | `groq/compound-mini` |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота для команд и охраны | `—` (слушатель не стартует) |
| `TELEGRAM_CHAT_ID` | Авторизованный Chat ID пользователя | `—` (слушатель не стартует) |
| `XIAOMI_BULB_IP` | IP-адрес умной лампы | `—` |
| `XIAOMI_BULB_TOKEN` | Токен miio (если пустой — протокол Yeelight) | `—` |
| `ATTENTION_TIMEOUT` | Длительность сессии внимания в секундах | `12` |
| `SEARCH_PROVIDER` | Поисковик: `yandex` или `google` | `yandex` |
| `MAPS_PROVIDER` | Карты: `yandex` или `google` | `yandex` |
| `AI_PROVIDER` | Браузерный ИИ по команде «открой нейросеть» | `yandex` (Алиса) |
| `DEFAULT_CITY` | Город по умолчанию для прогноза погоды | `Москва` |
| `PIPER_MODEL` | Имя .onnx файла голоса в `piper/models/` | `ru_RU-dmitri-medium.onnx` |
| `VOICE_SPEED` | Скорость синтеза речи (1.0 = стандартная) | `1.0` |

---

## Управление через systemd

Служба: `voice-assistant.service`  
Файл: `~/.config/systemd/user/voice-assistant.service`

```bash
systemctl --user daemon-reload
systemctl --user enable voice-assistant.service
systemctl --user start voice-assistant.service
systemctl --user restart voice-assistant.service
systemctl --user status voice-assistant.service
journalctl --user -u voice-assistant.service -f
```

Перезапуск голосом: «перезагрузись», «перезапустись», «рестарт» — выполняет `systemctl --user restart voice-assistant.service`.

---

## Основные команды

- **Активация:** «Джарвис» или «Умник».
- **Сессия и прерывания:** «стоп» (аварийная остановка), «замолчи» (пауза речи), «спать» (уход в ожидание), «тишина» (стоп музыки).
- **Погода:** «какая погода», «погода в Санкт-Петербурге», «будет ли дождь завтра».
- **Таймеры:** «поставь таймер на 5 минут», «сколько осталось на таймере», «сбрось таймер».
- **Калькулятор:** «сколько будет 25 умножить на 4», «корень из 144», «20 процентов от 500».
- **Развлечения:** «расскажи анекдот», «интересный факт», «скажи тост», «расскажи сказку».
- **Музыка и звуки:** «включи музыку», «включи радио Рекорд», «включи шум дождя», «пауза», «следующий трек».
- **Поиск и карты:** «найди в интернете [запрос]», «где находится [адрес]», «как проехать до [место]».
- **Охрана и умный дом:** «включи охрану», «джарвис я тут», «включи свет», «яркость 50».
- **Свободный диалог:** любые вопросы, поддержание беседы, рассуждения через Groq LLM.

Полный список фраз и логика их обработки приведены в файле `commands.txt`.
