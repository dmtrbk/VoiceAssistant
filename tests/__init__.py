# tests/__init__.py
# Импортируется раньше любого тестового модуля, поэтому здесь глушим сеть.
# Навыки шлют уведомления из фоновых потоков: прогон тестов уходил в реальный
# чат Telegram боевым токеном из .env, а тест при этом оставался зелёным.
# Смотрим на адрес назначения, а не на сокет: при HTTP(S)_PROXY соединение
# идёт на локальный прокси, и проверка сокета пропустила бы запрос наружу.

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

_AF_UNIX = getattr(socket, "AF_UNIX", None)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


class BlockedNetworkCall(RuntimeError):
    """Тест полез в интернет — значит, внешнему вызову не хватает мока."""


def _refuse(what: str, host: str):
    return BlockedNetworkCall(
        f"{what} к '{host}' из тестов запрещён: замокай внешний вызов. "
        f"Для намеренного похода в сеть — ALLOW_TEST_NETWORK=1."
    )


def _host_of_url(url: object) -> str:
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return str(url)


def _guard_url(original, what: str, url_of):
    def wrapper(*args, **kwargs):
        host = _host_of_url(url_of(*args, **kwargs))
        if host not in _LOCAL_HOSTS:
            raise _refuse(what, host)
        return original(*args, **kwargs)

    return wrapper


def _guard_socket(original, name: str):
    def wrapper(self, address, *args, **kwargs):
        if _AF_UNIX is not None and self.family == _AF_UNIX:
            return original(self, address, *args, **kwargs)
        host = str(address[0]) if isinstance(address, tuple) and address else str(address)
        if host not in _LOCAL_HOSTS:
            raise _refuse(f"socket.{name}", host)
        return original(self, address, *args, **kwargs)

    return wrapper


def _install_guards() -> None:
    # requests и httpx видят целевой URL до подстановки прокси — это основной барьер.
    try:
        from requests.adapters import HTTPAdapter

        HTTPAdapter.send = _guard_url(
            HTTPAdapter.send, "HTTP-запрос", lambda _self, request, *a, **kw: request.url
        )
    except ImportError:
        pass

    try:
        import httpx

        httpx.Client.send = _guard_url(
            httpx.Client.send, "HTTP-запрос", lambda _self, request, *a, **kw: request.url
        )
    except ImportError:
        pass

    import urllib.request

    urllib.request.urlopen = _guard_url(
        urllib.request.urlopen,
        "urlopen",
        lambda url, *a, **kw: getattr(url, "full_url", url),
    )

    # Всё остальное (сырые сокеты, miio, zeroconf) — по адресу соединения.
    socket.socket.connect = _guard_socket(socket.socket.connect, "connect")
    socket.socket.connect_ex = _guard_socket(socket.socket.connect_ex, "connect_ex")


if os.getenv("ALLOW_TEST_NETWORK", "").strip().lower() not in {"1", "true", "yes", "on"}:
    _install_guards()
