# -*- coding: utf-8 -*-
"""Regression test: an import must survive a dead CDP socket.

The bug (2026-09-14, seen as an endless "Transferring & verifying…"): WSL /
Lightpanda restarted under the relay, the daemon's WebSocket died, and every
/v1/session/import failed with WinError 10053 until something ELSE used the
CDP proxy. proxy_cdp already had resync+retry; the import path did not.
"""
import pathlib
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "relay"))
sys.path.insert(0, str(HERE.parent / "scripts"))

import server  # noqa: E402
import websocket  # noqa: E402


class TestImportSurvivesDeadSocket(unittest.TestCase):
    def test_import_retries_through_resync_on_dead_socket(self):
        """First _apply_session dies on the dead socket; the resync revives the
        connection and the retry succeeds - the import must then complete."""
        sys.path.insert(0, str(HERE.parent / "tests"))
        from test_sessions import _FakeTransport
        fake_transport = _FakeTransport()
        applies = []

        def fake_apply(origin, cookies, storage=None):
            applies.append(1)
            if len(applies) == 1:
                raise OSError("[WinError 10053] dead socket")
            # like the real _apply_session: the page now holds those cookies
            fake_transport.cookies = list(cookies)
            server._LAST_STORAGE_EXPECTED = len(storage or {})
            return len(storage or {})

        orig_apply = server._apply_session
        orig_resync = server._connection_resync
        orig_ensure = server._ensure_connection
        orig_transport = server._CDP_TRANSPORT
        orig_session = server._CDP_SESSION_ID
        try:
            server._apply_session = fake_apply
            server._ensure_connection = lambda origin: None
            resynced = []
            server._connection_resync = lambda: resynced.append(1)
            # the resync rebuilds the transport: a fake one so the post-apply
            # Network.getCookies verification has something to answer with
            server._CDP_TRANSPORT = fake_transport
            server._CDP_SESSION_ID = "fake-session"
            cookie_count, storage_count = server.set_session(
                "https://github.com",
                [{"name": "t", "value": "x", "domain": ".github.com", "path": "/",
                  "secure": True, "httpOnly": False, "sameSite": "no_restriction",
                  "expirationDate": 9999999999}],
                {})
            self.assertEqual(cookie_count, 1)
            self.assertEqual(len(applies), 2)
            self.assertEqual(len(resynced), 1)
        finally:
            server._apply_session = orig_apply
            server._connection_resync = orig_resync
            server._ensure_connection = orig_ensure
            server._CDP_TRANSPORT = orig_transport
            server._CDP_SESSION_ID = orig_session

    def test_a_non_connection_error_is_not_masked(self):
        """A ValueError from a bad origin must still fail loudly - the retry
        only covers connection loss, nothing else."""
        with self.assertRaises(ValueError):
            server.set_session("not-a-valid-origin", [{"name": "t", "value": "x"}])


    def test_inject_storage_propagates_a_dead_socket(self):
        """THE root cause of "0/8 keys" on a dead socket. _inject_storage used to
        catch Exception and store "<verify failed: WinError 10053>" as a key name,
        so set_session's resync never saw a connection loss, and the popup printed
        raw localized Windows text to the user."""
        sys.path.insert(0, str(HERE.parent / "tests"))
        from test_sessions import _FakeTransport

        class DeadSocket(_FakeTransport):
            def request(self, method, params=None, session_id=None):
                raise OSError(10053, "connexion abandonnee par l'hote")

        orig_transport = server._CDP_TRANSPORT
        orig_session = server._CDP_SESSION_ID
        try:
            server._CDP_TRANSPORT = DeadSocket()
            server._CDP_SESSION_ID = "s"
            with self.assertRaises(OSError):
                server._inject_storage("https://x.com", {"k": "v"})
        finally:
            server._CDP_TRANSPORT = orig_transport
            server._CDP_SESSION_ID = orig_session

    def test_page_level_failure_records_a_code_not_os_text(self):
        """A page-level evaluate failure stays retryable, but what reaches the
        popup is a short code - never the raw OS sentence (2026-09-14 screenshot)."""
        sys.path.insert(0, str(HERE.parent / "tests"))
        from test_sessions import _FakeTransport

        class BadPage(_FakeTransport):
            def request(self, method, params=None, session_id=None):
                raise Exception("Cannot access contents of the page. "
                                "Extension manifest must request permission...")

        orig_transport = server._CDP_TRANSPORT
        orig_session = server._CDP_SESSION_ID
        try:
            server._CDP_TRANSPORT = BadPage()
            server._CDP_SESSION_ID = "s"
            count = server._inject_storage("https://x.com", {"k": "v"})
            self.assertEqual(count, -1)
            joined = " ".join(server._LAST_STORAGE_MISSING)
            self.assertNotIn("WinError", joined)
            self.assertNotIn("permission", joined.lower())
            self.assertIn("verify-error:", joined)
        finally:
            server._CDP_TRANSPORT = orig_transport
            server._CDP_SESSION_ID = orig_session


if __name__ == "__main__":
    unittest.main(verbosity=2)