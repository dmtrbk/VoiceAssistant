# Linear USDT Perpetual contour (Bybit v5)

Отдельный модуль. **Спот-алго не трогаем** — базовый слой (WS spot / IOC / reconcile / Earn / журналы) остаётся.

## Запуск

```bash
# Paper (Core-2 ZEC+HYPE, shadow Core-3b +AAVE)
.venv/bin/python -m skills.crypto.linear.runner --paper

# Live (нужны BYBIT_API_KEY/SECRET; One-Way Isolated 2x)
.venv/bin/python -m skills.crypto.linear.runner --live --sizing risk_clamp

# Parity audit 48h
.venv/bin/python -m skills.crypto.linear.runner --audit --hours 48
```

## Ядро

- `strategy_core.py` — фичи/сигналы +H1+H2+TP2.5R (rolling_4320), общий для backtest/live
- Parity check: `research_linear_parity_core.py`

## Журналы

`research_linear_data/linear_runtime/`
- `paper_signals.jsonl` / `paper_trades.jsonl`
- `shadow_aave_signals.jsonl` / `shadow_aave_trades.jsonl`
