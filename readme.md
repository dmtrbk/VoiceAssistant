# Джарвис — голосовой ассистент для Manjaro GNOME

Локальный ассистент под **Manjaro + GNOME** (Wayland / PipeWire): wake-words и стоп через **Vosk**, уточнение фраз через **Groq Whisper**, речь — **Piper (Dmitri)**, диалог — **Groq**, навыки — музыка, окна, биржа, крипта, умный дом, Telegram.

Точки входа:

| Команда | Назначение |
|---------|------------|
| `python assistant.py` | Микрофон, Vosk, Piper, сфера, Telegram |
| `python assistant.py --no-gui` | То же без виджета |
| `python cli.py` | Текст в терминале (с озвучкой) |
| `python cli.py --mute` | Только текст |
| `python cli.py "какая погода"` | Одна команда и выход |

Служба пользователя: `voice-assistant.service` (ставит `setup.sh`).

Полный список фраз — в [`commands.txt`](commands.txt).

---

## Возможности (кратко)

- **Сессия:** имена «джарвис / умник / гаврила / гаврюша», тайм-аут внимания, barge-in («стоп», «замолчи», имя), ремонт диалога («что?», «продолжай»), ducking Audacious.
- **STT:** `hybrid` (Vosk + Whisper в фоне) или `vosk`; режим в настройках сферы.
- **Характер:** пресеты в GUI / голосом (классический, саркастичный, брутальный, бро, свой промпт).
- **Навыки:** погода, таймеры, калькулятор, музыка и радио, фильмы (MPV), поиск и карты, картинки (Cloudflare Flux), охрана с камерой, Xiaomi / Home Assistant, окна GNOME, Википедия.
- **Биржа (Т-Инвест) и крипта (Bybit):** котировки, сводка, сделки голосом и автостол — отдельные тумблеры; по умолчанию сделки выкл. Conky в правом нижнем углу — дневной +/-.
- **Каналы:** один маршрутизатор для голоса, CLI и Telegram.

---

## Установка

На новой машине **не копируют `.venv`**. С GitHub — исходник, окружение собирается заново.

```bash
git clone https://github.com/dmtrbk/VoiceAssistant.git
cd VoiceAssistant
git checkout experimental   # актуальная ветка разработки
chmod +x setup.sh setup_echo_cancel.sh
./setup.sh
```

Что делает `setup.sh`:

1. Пакеты через `pacman` (PipeWire, Audacious, MPV, yt-dlp, Conky и др.).
2. Создаёт `.venv` и ставит зависимости из `requirements.txt`.
3. Скачивает Vosk (`model/`) и Piper (`piper/`).
4. Создаёт `.env` из `.env.example`, если файла ещё нет, и ставит права **`chmod 600`**.
5. Кладёт виджет рынков в `~/.conky`.
6. Включает `~/.config/systemd/user/voice-assistant.service`.

Дальше заполните `.env` (минимум `GROQ_API_KEY`). Остальное — по желанию: Telegram, Т-Инвест, Bybit, Cloudflare, HA, лампа.

### Переезд на другое железо

Со старой машины — только секреты и память, не `.venv`:

```bash
# после clone + ./setup.sh на новой
scp старый:~/VoiceAssistant/.env ~/VoiceAssistant/.env
chmod 600 ~/VoiceAssistant/.env
scp старый:~/VoiceAssistant/skills_enabled.json ~/VoiceAssistant/
scp старый:~/VoiceAssistant/jarvis_{holds,bought,trades}.json \
    старый:~/VoiceAssistant/jarvis_crypto_{holds,bought,trades}.json \
    ~/VoiceAssistant/
systemctl --user restart voice-assistant.service
```

### Эхоподавление

```bash
./setup_echo_cancel.sh
```

Ставит микрофон с WebRTC AEC по умолчанию. Опорный сигнал — текущий выход колонок / HDMI / Bluetooth.

### Служба

```bash
systemctl --user status voice-assistant.service
systemctl --user restart voice-assistant.service
journalctl --user -u voice-assistant.service -f
```

Голосом: «перезапустись», «рестарт», «перезапусти ассистента».

---

## Настройка `.env`

Шаблон — [`.env.example`](.env.example). Файл `.env` в git не попадает; после создания права должны быть `600`.

Главное:

```ini
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b
STT_MODE=hybrid          # hybrid | vosk
PERSONA_PRESET=jarvis    # jarvis | sarcastic | brutal | buddy | custom

# Биржа (опционально). Сделки голосом по умолчанию выкл.
TINKOFF_TOKEN=
TINKOFF_SANDBOX=false
TINKOFF_VOICE_TRADE=
TINKOFF_AUTO_TRADE=
TINKOFF_WATCHLIST=SBER,LKOH,YDEX,VTBR

# Крипта Bybit (опционально)
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_TESTNET=false
CRYPTO_VOICE_TRADE=
CRYPTO_AUTO_TRADE=

# Картинки Cloudflare Workers AI
CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_API_TOKEN=

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

ATTENTION_TIMEOUT=12
ATTENTION_TIMEOUT_MUSIC=5
DUCKING_VOLUME=8
DUCKING_MODE=duck
SEARCH_PROVIDER=yandex
MAPS_PROVIDER=yandex
```

Живой токен Т-Инвест с правом торговли при включённых сделках выставляет **реальные** заявки. Для отладки — `TINKOFF_SANDBOX=true`. То же для крипты: на боевом Bybit осторожно; безопаснее сначала `BYBIT_TESTNET=true`.

Тумблеры в GUI (правый клик по сфере или «открой настройки») пишут `skills_enabled.json` и важнее пустых флагов в `.env` после рестарта процесса.

---

## Структура репозитория

```
VoiceAssistant/
├── assistant.py           # Оркестратор: Vosk, Piper, сессия, уточнение STT в фоне
├── stt.py                 # Буфер фразы, Whisper, fallback на Vosk
├── cli.py                 # Текстовый режим
├── commands.py            # Маршрутизатор навыков
├── dialogue_repair.py     # Паузы, «что?», голые глаголы
├── context_manager.py     # FSM / подтверждения (в т.ч. выключение ПК)
├── telegram_listener.py
├── tts_cache.py
├── volume_control.py      # Ducking Audacious
├── window_control.py      # Окна GNOME
├── indicator.py / settings_ui.py / skill_settings.py
├── signal_advisor.py / crypto_advisor.py / conky_markets.py
├── setup.sh / setup_echo_cancel.sh
├── commands.txt           # Справочник фраз
├── requirements.txt / .env.example
├── certs/                 # Корневой CA Минцифры для Т-Инвест
├── gnome/                 # Расширение Shell для окон
├── conky/                 # Шаблон виджета рынков
├── tests/                 # Unit-тесты (сеть из тестов запрещена)
└── skills/
    ├── stocks/            # Т-Инвест: quotes, trades, desk, journal
    ├── crypto/            # Bybit: то же разбиение
    ├── ai_chat.py         # Groq-диалог
    ├── persona.py         # Характеры
    └── …                  # Погода, музыка, охрана, HA, …
```

Каталоги `model/` и `piper/` в git не лежат — их качает установщик.

---

## Тесты и CI

Локально (обязательно с `-t .`, иначе не подхватится запрет сети в `tests/__init__.py`):

```bash
source .venv/bin/activate
python -m unittest discover -s tests -t .
```

Намеренный выход в сеть из тестов: `ALLOW_TEST_NETWORK=1`.

На GitHub (ветки `main` и `experimental`): **Tests** и **CodeQL**. В Actions для тестов ставятся PortAudio и OpenGL — без них падает импорт `assistant` / PySide6.

CodeQL может пометить HMAC-SHA256 подписи Bybit как «слабый хеш пароля» — это ложное срабатывание: так требует протокол биржи, не хранение паролей.

---

## Безопасность (кратко)

- `.env` — только владелец (`chmod 600`), не коммитить.
- Сделки голосом и автоторговля — отдельные тумблеры, по умолчанию выкл.
- «Выключи компьютер» сначала спрашивает подтверждение («Точно выключить компьютер?»).
- Снимки охраны пишутся в `surveillance_snaps/` и уходят в Telegram — следите за диском и чатом.
