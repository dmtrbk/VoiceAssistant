# Локальный архив спот-стола (2026-11-24)

Снимок **торгового алго** (щуп/карманы/ребаланс) до перехода на USDT Perpetual.

В рантайме `skills/crypto/` **оставлен базовый слой**:
- WS-лента (`stream` / `_ensure_tape`)
- Limit IOC / post-only обёртки (`trades.py`)
- reconcile с биржей
- раздельные журналы
- Earn-парковка

Снято с рантайма: автозаявки щупа/карманов (`_auto_trade_enabled=False`,
`probe_places_buys=False`, desk-loop без `_probe_cycle`).

Скопировано на момент архива:
- `desk.py`, `desk_policy.py`, `desk_policy_legacy.py`
- `trades.py`, `journal.py`, `quotes.py`, `common.py`, `crypto_ai_radar.py`
- `crypto_backtest.py`, `crypto_desk_full_replay.py`, `crypto_desk_replay.py`
- `crypto_probe_bybit_backtest.py`, `crypto_probe_top_gainers_sweep.py`
