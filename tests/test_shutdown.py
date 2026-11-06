"""The server has to stop when it is told to.

A container is stopped with SIGTERM, and the default answer to SIGTERM
is to die on the spot - which would cut a poll short and leave the
history database to be closed by the operating system rather than by
the code that opened it.

Ember handles it instead. These tests check that the signal does what
it says, and - the part that is easy to get wrong - that it does not
deadlock: `shutdown()` waits for `serve_forever()` to return, so it
cannot be called from the thread that is running it.
"""

import os
import signal
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from ember.config import Settings
from ember.dashboard import Dashboard
from ember.demo import DemoProvider
from ember.history import History
from ember.server import EmberServer, _install_signal_handlers


def free_port() -> int:
    """A port nothing is using, so the test can bind it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StoppingOnASignalTests(unittest.TestCase):
    """SIGTERM has to stop the server, and stop it quickly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.settings = Settings(data / "settings.json")
        self.settings.set("demo", True)
        self.history = History(data / "history.db")
        provider = DemoProvider()
        provider.seed(self.history)
        self.dashboard = Dashboard(self.settings, self.history, provider)
        self.port = free_port()
        self.server = None
        self.old_handlers = {}

    def tearDown(self):
        for number, handler in self.old_handlers.items():
            signal.signal(number, handler)
        if self.server is not None:
            self.server.server_close()
        self.history.close()
        self.tmp.cleanup()

    def _remember_handlers(self):
        """Put the old handlers back afterwards.

        A test that leaves a SIGTERM handler behind would change how
        the rest of the suite behaves, and one that leaves the default
        in place would kill the test run.
        """
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(signal, name, None)
            if number is not None:
                self.old_handlers[number] = signal.getsignal(number)

    def _wait_for_the_port(self, timeout=10.0):
        """Wait until the server is actually listening."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port),
                                              timeout=1):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def test_the_handlers_are_installed(self):
        """SIGTERM and SIGINT are handled, not left to the default."""
        self._remember_handlers()
        self.server = EmberServer(("127.0.0.1", self.port),
                                  self.settings, self.dashboard)
        _install_signal_handlers(self.server)
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            self.assertNotEqual(signal.getsignal(number), signal.SIG_DFL,
                                f"{name} was left to its default")

    def test_sigterm_stops_the_server(self):
        """The signal stops it, and it stops promptly."""
        self._remember_handlers()
        self.server = EmberServer(("127.0.0.1", self.port),
                                  self.settings, self.dashboard)
        # The handlers have to be installed on the main thread - that is
        # where the application runs them, and where a signal can be
        # caught at all.
        _install_signal_handlers(self.server)
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.assertTrue(self._wait_for_the_port(), "the server never started")

        os.kill(os.getpid(), signal.SIGTERM)

        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(),
                         "the server did not stop when it was told to")

    def test_a_stopped_server_releases_its_port(self):
        """A redeploy needs the port back."""
        self._remember_handlers()
        self.server = EmberServer(("127.0.0.1", self.port),
                                  self.settings, self.dashboard)
        _install_signal_handlers(self.server)
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.assertTrue(self._wait_for_the_port(), "the server never started")
        os.kill(os.getpid(), signal.SIGTERM)
        thread.join(timeout=15)
        self.server.server_close()
        self.server = None

        # The port is free again, so the next container can take it.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.port))
