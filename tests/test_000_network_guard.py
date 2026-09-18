# tests/test_000_network_guard.py
# `unittest discover -s tests` без `-t .` берёт каталог как корень и не импортирует
# tests/__init__.py, где стоит запрет сети. Этот модуль грузится первым по алфавиту,
# втягивает пакет и заодно проверяет, что запрет действительно работает.

import os
import unittest

import tests as tests_package

_ALLOWED = os.getenv("ALLOW_TEST_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipIf(_ALLOWED, "сеть намеренно разрешена через ALLOW_TEST_NETWORK")
class TestNetworkGuard(unittest.TestCase):
    def test_requests_to_internet_blocked(self):
        import requests

        with self.assertRaises(tests_package.BlockedNetworkCall):
            requests.post("https://api.telegram.org/botX/sendMessage", timeout=1)

    def test_raw_socket_to_internet_blocked(self):
        import socket

        with self.assertRaises(tests_package.BlockedNetworkCall):
            socket.create_connection(("1.1.1.1", 443), timeout=1)

    def test_localhost_still_allowed(self):
        import socket

        # Гард не должен мешать локальным сокетам: ConnectionRefused значит, что пропустил.
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", 1), timeout=1)
