"""SignalService/GetSignals через gRPC — запасной путь, когда REST-прокси Т-Инвест отвечает ошибкой.

Без сгенерированных stub'ов: запрос и ответ кодируются вручную по signals.proto,
поэтому достаточно пакета grpcio.
"""

from __future__ import annotations

from typing import Any

import certifi

from .common import _sandbox, _tinkoff_verify

_HOST_PROD = "invest-public-api.tinkoff.ru:443"
_HOST_SANDBOX = "sandbox-invest-public-api.tinkoff.ru:443"
_METHOD = "/tinkoff.public.invest.api.contract.v1.SignalService/GetSignals"

_DIRECTION_BUY = 1
_STATE_ACTIVE = 1
_DIRECTIONS = {1: "SIGNAL_DIRECTION_BUY", 2: "SIGNAL_DIRECTION_SELL"}


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def encode_request(limit: int, page: int = 0) -> bytes:
    """GetSignalsRequest: direction=7, active=8, paging=9 {limit=1, page_number=2}."""
    paging = _key(1, 0) + _varint(limit) + _key(2, 0) + _varint(page)
    return (
        _key(7, 0) + _varint(_DIRECTION_BUY)
        + _key(8, 0) + _varint(_STATE_ACTIVE)
        + _key(9, 2) + _varint(len(paging)) + paging
    )


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, pos


def _fields(buf: bytes) -> list[tuple[int, Any]]:
    out: list[tuple[int, Any]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            size, pos = _read_varint(buf, pos)
            value = buf[pos:pos + size]
            pos += size
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"wire type {wire}")
        out.append((field, value))
    return out


def _signed(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _quotation(buf: bytes) -> dict[str, Any]:
    parts = dict(_fields(buf))
    return {"units": str(_signed(int(parts.get(1, 0)))), "nano": _signed(int(parts.get(2, 0)))}


def decode_signals(buf: bytes) -> list[dict[str, Any]]:
    """GetSignalsResponse.signals (поле 1) → словари в форме REST-ответа."""
    signals: list[dict[str, Any]] = []
    for field, raw in _fields(buf):
        if field != 1:
            continue
        sig: dict[str, Any] = {}
        for num, value in _fields(raw):
            if num == 1:
                sig["signalId"] = value.decode()
            elif num == 2:
                sig["strategyId"] = value.decode()
            elif num == 3:
                sig["strategyName"] = value.decode()
            elif num == 4:
                sig["instrumentUid"] = value.decode()
            elif num == 6:
                sig["direction"] = _DIRECTIONS.get(int(value), "SIGNAL_DIRECTION_UNSPECIFIED")
            elif num == 7:
                sig["initialPrice"] = _quotation(value)
            elif num == 8:
                sig["info"] = value.decode()
            elif num == 9:
                sig["name"] = value.decode()
            elif num == 10:
                sig["targetPrice"] = _quotation(value)
            elif num == 12:
                sig["probability"] = _signed(int(value))
        signals.append(sig)
    return signals


def _root_certificates() -> bytes:
    with open(certifi.where(), "rb") as handle:
        roots = handle.read()
    extra = _tinkoff_verify()
    if isinstance(extra, str):
        with open(extra, "rb") as handle:
            roots += b"\n" + handle.read()
    return roots


def fetch_buy_signals(token: str, limit: int, page: int = 0, timeout: float = 15.0) -> list[dict[str, Any]]:
    import grpc

    host = _HOST_SANDBOX if _sandbox() else _HOST_PROD
    channel = grpc.secure_channel(host, grpc.ssl_channel_credentials(root_certificates=_root_certificates()))
    try:
        call = channel.unary_unary(_METHOD)
        response = call(
            encode_request(limit, page),
            metadata=[("authorization", f"Bearer {token}")],
            timeout=timeout,
        )
    finally:
        channel.close()
    return decode_signals(response)
