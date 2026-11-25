# Linear USDT Perpetual contour (Bybit v5)

Отдельный модуль. **Спот-алго не трогаем** — базовый слой (WS spot / IOC / reconcile / Earn / журналы) остаётся.

## Запуск

## Автостарт с ассистентом

При `desk_enabled` в настройках стола `crypto_skill.start_background()` поднимает
`linear.service` в daemon-потоке Джарвиса. Закрытие Cursor стол не роняет.
Выкл: тумблер «Linear-контур» или навык Крипта off.

Warm истории — **один раз** на старте. В рантайме HTF только `limit=5` (без полной перекачки).
Pending TTL = 2×15m до новых сигналов. Core/Shadow — отдельные portfolio.

Проверки:
```bash
.venv/bin/python -m unittest tests.test_linear_infra -v
.venv/bin/python -m skills.crypto.linear.runner --audit --hours 30 --both
```

Ручной CLI (отладка, без ассистента):

```bash
# Paper — без средств/ключей на ордера; публичные свечи + WS
# Виртуальный старт $120 (можно --equity 120)
.venv/bin/python -m skills.crypto.linear.runner --paper --equity 120

# Live (нужны BYBIT_API_KEY/SECRET; One-Way Isolated 2x)
.venv/bin/python -m skills.crypto.linear.runner --live --sizing risk_clamp

# Parity audit 48h
.venv/bin/python -m skills.crypto.linear.runner --audit --hours 48
```

Paper пишет в `research_linear_data/linear_runtime/`:
- `logs/desk.log` (+ ротация `.1`…`.12`) — текстовый лог процесса
- `paper_signals.jsonl` / `paper_trades.jsonl` / `paper_events.jsonl` / `paper_daily.jsonl`
- shadow: `shadow_aave_*.jsonl`

Каталог в `.gitignore` (локально сохраняется). Другой путь: `LINEAR_LOG_DIR=~/Документы/stol/linear`.

Сигналы только на закрытии 15m — после warm (несколько минут) ждём бар.

## Ядро

- `strategy_core.py` — фичи/сигналы +H1+H2+TP2.5R (rolling_4320), общий для backtest/live
- Parity check: `research_linear_parity_core.py`

## Журналы

`research_linear_data/linear_runtime/`
- `paper_signals.jsonl` / `paper_trades.jsonl`
- `shadow_aave_signals.jsonl` / `shadow_aave_trades.jsonl`
