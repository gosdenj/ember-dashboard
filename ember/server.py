"""The web server.

Standard library only: ``ThreadingHTTPServer`` for the HTTP side and a
handful of Server-Sent Events for the live updates. No framework, no
template engine, no build step.

The server binds to localhost by default. The settings page can write the
Hive password and the Octopus API key, so exposing it to the network is
something to do deliberately (``--host 0.0.0.0``), not something to do
by accident.
"""

from __future__ import annotations

import json
import sys
import mimetypes
import os
import queue
import signal
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .discover import (DiscoveryError, HiveFlows, discover_octopus,
                       discover_place)
from .hive import HiveError
from .octopus import OctopusError

WEB = Path(__file__).parent / "web"

# The fonts are the one thing the panel cannot do without, so their
# content type is stated rather than guessed. The server sends
# nosniff, and a browser told that a font is octet-stream is entitled
# to refuse it - and Python's own MIME table does not know about
# .woff2 on every version, or without the system's media types file.
CONTENT_TYPES = {
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


class Emitter:
    """One Server-Sent Events client."""

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue(maxsize=32)

    def put(self, payload: dict) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            # A client that cannot keep up gets the next one instead of
            # stalling the dashboard for everyone else.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except queue.Empty:
                pass

    def get(self, timeout: float = 15.0) -> dict | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


class Handler(BaseHTTPRequestHandler):
    server_version = "Ember"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = CONTENT_TYPES.get(path.suffix.lower()) \
            or mimetypes.guess_type(path.name)[0] \
            or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, content_type)

    # -- routes -----------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._file(WEB / "index.html")
            return
        if route in ("/settings", "/settings.html"):
            self._file(WEB / "settings.html")
            return
        if route.startswith("/static/"):
            name = route[len("/static/"):]
            if "/" in name or ".." in name:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._file(WEB / name)
            return
        if route.startswith("/fonts/"):
            name = route[len("/fonts/"):]
            if "/" in name or ".." in name:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._file(WEB / "fonts" / name)
            return
        if route == "/api/state":
            self._json(self.server.dashboard.payload())  # type: ignore[attr-defined]
            return
        if route == "/api/settings":
            self._json(self.server.settings.public())  # type: ignore[attr-defined]
            return
        if route == "/api/discover/status":
            self._json(self.server.status())  # type: ignore[attr-defined]
            return
        if route == "/api/events":
            self._events()
            return
        if route == "/healthz":
            self._json({"ok": True, "version": self.server.version})  # type: ignore[attr-defined]
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route not in ("/api/settings", "/api/discover/hive",
                         "/api/discover/hive/code", "/api/discover/octopus",
                         "/api/discover/place", "/api/discover/refresh",
                         "/api/history/prune"):
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json({"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length < 0:
            # A negative length would ask the socket for everything
            # until the connection closed, which hangs the request.
            self._json({"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length > 64 * 1024:
            self._json({"error": "too big"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(body, dict):
            self._json({"error": "expected an object"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            if route == "/api/settings":
                self._save_settings(body)
                return
            if route == "/api/discover/hive":
                self._discover_hive(body)
                return
            if route == "/api/discover/hive/code":
                self._discover_hive_code(body)
                return
            if route == "/api/discover/octopus":
                self._discover_octopus(body)
                return
            if route == "/api/discover/place":
                self._discover_place(body)
                return
            if route == "/api/history/prune":
                self._prune_history()
                return
            self._refresh()
        except Exception as exc:
            # A surprise must not take the connection down with it. The
            # message is scrubbed of the secrets first, because an
            # exception is the one place a password could turn up by
            # accident.
            self._json({"ok": False, "error": self._scrub(str(exc))},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- writing ----------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """Take the secrets out of a message before it leaves the process.

        An exception message is the one place a password could turn up
        by accident, and this is the one place a message is shown to
        somebody.
        """
        settings = self.server.settings  # type: ignore[attr-defined]
        for key in ("hive_password", "octopus_api_key"):
            secret = str(settings.get(key) or "")
            if len(secret) >= 6 and secret in text:
                text = text.replace(secret, "***")
        return text

    def _prune_history(self) -> None:
        """Delete the history that has fallen out of the retention window.

        The same thing the poller does once a day, on demand - so
        somebody who has just changed the retention setting does not
        have to wait a day for it to take effect.
        """
        server = self.server  # type: ignore[attr-defined]
        settings = server.settings
        history = getattr(server.dashboard, "history", None)
        if history is None:
            self._json({"ok": False, "error": "no history store"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            removed = history.prune(settings.get_int("history_days"),
                                    settings.get_int("trv_days"))
        except Exception as exc:
            self._json({"ok": False, "error": self._scrub(str(exc))},
                       HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        stats = getattr(server.dashboard, "history_stats", None)
        payload = {"ok": True, "removed": removed}
        if callable(stats):
            try:
                payload["history"] = stats()
            except Exception:
                pass
        self._json(payload)

    def _save_settings(self, body: dict) -> None:
        settings = self.server.settings  # type: ignore[attr-defined]
        try:
            settings.update(body)
            settings.save()
        except KeyError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"ok": True, "settings": settings.public()})

    def _apply(self, discovered: dict) -> None:
        """Store what was found and tell the dashboard to have another look."""
        settings = self.server.settings  # type: ignore[attr-defined]
        try:
            settings.update(discovered.get("settings") or {})
        except KeyError as exc:
            raise DiscoveryError(
                f"the discovery produced a setting Ember does not know "
                f"about: {exc}") from exc
        settings.save()
        self._poll_async()

    def _poll_async(self) -> None:
        """Ask the dashboard to have another look, without waiting for it.

        A poll goes out to Hive, to Octopus and to the weather service,
        so it takes as long as those take. The settings page's answer to
        "did it find my meter?" does not depend on any of that, so the
        page is answered straight away and the dashboard catches up.

        The thread is remembered so the server can wait for it on the
        way out. A poll that is still running when the history database
        is closed takes the process down with it.
        """
        def run() -> None:
            try:
                self.server.dashboard.poll()  # type: ignore[attr-defined]
            except Exception:
                # A failed poll is the dashboard's problem to report, not
                # the settings page's - the settings were saved either way.
                pass

        thread = threading.Thread(target=run, daemon=True)
        self.server.pollers.append(thread)  # type: ignore[attr-defined]
        thread.start()



    # -- discovery --------------------------------------------------------

    def _discover_hive(self, body: dict) -> None:
        email = str(body.get("email") or "")
        password = str(body.get("password") or "")
        settings = self.server.settings  # type: ignore[attr-defined]
        if not email:
            email = settings.get("hive_email", "")
        if not password:
            # The password is in the settings file and is never sent to
            # the browser, so the box on the page is empty every time.
            # An empty box therefore means "use the one you have" -
            # which is what makes signing in again, after a session has
            # finally died, a one-click job rather than a retype.
            password = settings.get("hive_password", "")
        if not email or not password:
            self._json({"ok": False,
                        "error": "an email address and a password are both needed"},
                       HTTPStatus.BAD_REQUEST)
            return
        try:
            result = self.server.flows.start(email, password)  # type: ignore[attr-defined]
        except DiscoveryError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except HiveError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if result.get("status") == "signed_in":
            self._save_hive_credentials(email, password)
            self._after_hive_sign_in(result)
        else:
            # Hive sent a code, which means it accepted the password.
            # Keeping it means the code is the only thing left to type.
            self._save_hive_credentials(email, password)
        self._json({"ok": True, "result": public_result(result),
                    "settings": self.server.settings.public()})  # type: ignore[attr-defined]

    def _discover_hive_code(self, body: dict) -> None:
        flow = str(body.get("flow") or "")
        code = str(body.get("code") or "")
        try:
            result = self.server.flows.complete_code(flow, code)  # type: ignore[attr-defined]
        except (DiscoveryError, HiveError) as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if result.get("status") == "signed_in":
            self._after_hive_sign_in(result)
        self._json({"ok": True, "result": public_result(result),
                    "settings": self.server.settings.public()})  # type: ignore[attr-defined]

    def _save_hive_credentials(self, email: str | None, password: str | None) -> None:
        """Keep the Hive login, so a later sign-in does not need it again."""
        settings = self.server.settings  # type: ignore[attr-defined]
        if email:
            settings.set("hive_email", email)
        if password:
            settings.set("hive_password", password)
        settings.save()

    def _after_hive_sign_in(self, result: dict) -> None:
        """The session is on disk now, so go and use it."""
        self._poll_async()

    def _discover_octopus(self, body: dict) -> None:
        key = str(body.get("api_key") or "")
        account = str(body.get("account") or "")
        current_unit = str(self.server.settings.get("gas_unit", "auto"))  # type: ignore[attr-defined]
        try:
            discovered = discover_octopus(key, account, current_unit)
        except (DiscoveryError, OctopusError) as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._apply(discovered)
        self._json({"ok": True, "summary": discovered.get("summary") or {},
                    "settings": self.server.settings.public()})  # type: ignore[attr-defined]

    def _discover_place(self, body: dict) -> None:
        postcode = str(body.get("postcode") or "")
        try:
            discovered = discover_place(postcode)
        except DiscoveryError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._apply(discovered)
        self._json({"ok": True, "summary": discovered.get("summary") or {},
                    "settings": self.server.settings.public()})  # type: ignore[attr-defined]

    def _refresh(self) -> None:
        """Ask the dashboard to poll right now, rather than waiting."""
        try:
            payload = self.server.dashboard.poll()  # type: ignore[attr-defined]
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)},
                       HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"ok": True, "state": payload})

    # -- server-sent events ----------------------------------------------

    def _events(self) -> None:
        emitter = Emitter()
        self.server.dashboard.subscribe(emitter.put)  # type: ignore[attr-defined]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            current = self.server.dashboard.payload()  # type: ignore[attr-defined]
            if current:
                self._write_event(current)
            while True:
                payload = emitter.get(timeout=15.0)
                if payload is None:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                self.server.dashboard.unsubscribe(emitter.put)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _write_event(self, payload: dict) -> None:
        body = json.dumps(payload, default=str)
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()


#: What may be said about a sign-in out loud. Everything else - the
#: tokens, the Cognito session, the canonical user id - stays in the
#: process. The refresh token is a long-lived credential for the Hive
#: account, so it has no business being in an HTTP response.
PUBLIC_RESULT_KEYS = ("status", "message", "flow", "rooms", "device",
                      "session_file")


def public_result(result: dict) -> dict:
    """Strip a sign-in result down to what the browser may see."""
    if not isinstance(result, dict):
        return {}
    return {key: result[key] for key in PUBLIC_RESULT_KEYS if key in result}


class EmberServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """A dropped connection is not a fault.

        The browser closes the event stream whenever it navigates away
        or the tab is reloaded, and the standard library turns that into
        a full traceback. Nobody needs to read a stack trace because
        their browser refreshed the page.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, address, settings, dashboard, version="0.1.0", verbose=False):
        super().__init__(address, Handler)
        self.settings = settings
        self.dashboard = dashboard
        self.version = version
        self.verbose = verbose
        #: Sign-ins waiting for a two-step code.
        self.flows = HiveFlows(session_path=self._session_path())
        #: Background polls, so the server can wait for them on the way out.
        self.pollers: list[threading.Thread] = []

    def _wait_for_pollers(self) -> None:
        """Let any background poll finish before the database is closed."""
        for thread in list(self.pollers):
            if thread.is_alive():
                thread.join(timeout=10)
        self.pollers.clear()

    def server_close(self) -> None:
        self._wait_for_pollers()
        super().server_close()

    def _session_path(self) -> str:
        """Where a new Hive session should be written."""
        finder = getattr(self.dashboard, "_session_paths", None)
        if finder is not None:
            try:
                paths = finder()
                if paths:
                    return paths[0]
            except Exception:
                pass
        from .discover import default_session_path
        return default_session_path()

    def status(self) -> dict:
        """What is set up, and what is still missing.

        The settings page opens with this, so the first thing it shows is
        the state of the world rather than an empty form.
        """
        settings = self.settings
        missing: list[str] = []
        # Only the things a person actually has to supply. The meter
        # point and the tariff are worked out from the API key, so they
        # are only "missing" when the key is there and the lookup did not
        # finish - which is a different problem, and saying so matters.
        if not settings.get("octopus_api_key"):
            missing.append("Octopus API key")
        elif not settings.get("gas_mprn"):
            missing.append("gas meter point (look it up with the key)")
        if not settings.get("postcode"):
            missing.append("postcode")
        if not settings.get("hive_email"):
            missing.append("Hive email")
        elif not self._hive_session_exists():
            missing.append("Hive sign-in")
        hive = {}
        status = getattr(self.dashboard, "hive_status", None)
        if callable(status):
            try:
                hive = status()
            except Exception:
                hive = {}
        history = {}
        stats = getattr(self.dashboard, "history_stats", None)
        if callable(stats):
            try:
                history = stats()
            except Exception:
                history = {}
        return {
            "missing": missing,
            "configured": not missing,
            "settings": settings.public(),
            "files": self.files(),
            "hive": hive,
            "history": history,
        }

    def _hive_session_exists(self) -> bool:
        dashboard = self.dashboard
        finder = getattr(dashboard, "_session_paths", None)
        if finder is None:
            return False
        try:
            paths = finder()
        except Exception:
            return False
        return any(Path(path).exists() for path in paths)

    def files(self) -> list[dict]:
        """Where Ember keeps its files, and whether they are there.

        Knowing the path is the difference between "the heating page is
        empty" and "the heating page is empty because this file is
        missing".
        """
        history = getattr(self.dashboard, "history", None)
        listed = [{"name": "settings", "path": str(self.settings.path)}]
        # Every place the app looks for a Hive session, not just the
        # first: "the heating page is empty" is usually "the session is
        # in the second place, not the first".
        finder = getattr(self.dashboard, "_session_paths", None)
        try:
            session_paths = finder() if finder else []
        except Exception:
            session_paths = []
        if not session_paths:
            session_paths = [self._session_path()]
        for index, path in enumerate(session_paths):
            listed.append({
                "name": "Hive session" if index == 0
                        else "Hive session (also looked in)",
                "path": str(path),
            })
        if history is not None and getattr(history, "path", None):
            listed.append({"name": "history", "path": str(history.path)})
        for entry in listed:
            try:
                entry["exists"] = Path(entry["path"]).exists()
            except OSError:
                entry["exists"] = False
        return listed


def _install_signal_handlers(server: EmberServer) -> None:
    """Stop cleanly when the process is told to stop.

    A container is stopped with SIGTERM, and the default answer to
    SIGTERM is to die on the spot - which would cut a poll short and
    leave the history database to be closed by the operating system
    rather than by the code that opened it.

    So SIGTERM is handled: the server is asked to stop, and it stops the
    way it stops for any other reason - the background polls are waited
    for and the database is closed.

    `shutdown()` waits for `serve_forever()` to return, so it must not
    be called from the thread that is running it - and a signal handler
    runs in exactly that thread. Calling it from a thread of its own is
    what keeps the stop from deadlocking.
    """
    def stop(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, stop)
        except (ValueError, OSError):
            # Not the main thread, or not a platform that has it. The
            # server still stops on Ctrl-C.
            pass


def serve(settings, dashboard, host: str = "127.0.0.1", port: int = 8766,
          verbose: bool = False, version: str = "0.1.0") -> None:
    server = EmberServer((host, port), settings, dashboard, version, verbose)
    print(f"Ember is on http://{host}:{port}")
    print(f"  settings   http://{host}:{port}/settings")
    if host not in ("127.0.0.1", "localhost"):
        print("  (the settings page holds your credentials and has no "
              "login - only share this on a network you trust)")
    _install_signal_handlers(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
