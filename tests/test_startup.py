"""The dashboard has to come up, whatever happens.

On 2026-11-05 the dashboard died on its first live poll - before the
server had even started - because one Octopus row had a timestamp in a
shape the parser did not know. The traceback was accurate and the
result was useless: a wall panel that refuses to start cannot even be
told what went wrong.

So the first poll is guarded. If it fails, the server still starts, the
problem is on the panel, and the settings page is reachable.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run  # noqa: E402


class ExplodingDashboard:
    """A dashboard whose first poll fails in a way nobody expected."""

    def __init__(self, settings, history, provider):
        self.settings = settings

    def poll(self):
        raise RuntimeError("a shape nobody has seen before")

    def start(self):
        pass

    def stop(self):
        pass


class WorkingDashboard(ExplodingDashboard):
    """A dashboard whose first poll is fine."""

    def poll(self):
        return {"gas": {}, "heating": {}}


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.original_dashboard = run.Dashboard
        self.original_serve = run.serve
        run.serve = lambda *args, **kwargs: None
        self.addCleanup(self._restore)
        import shutil
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _restore(self):
        run.Dashboard = self.original_dashboard
        run.serve = self.original_serve

    def _main(self, dashboard_class):
        run.Dashboard = dashboard_class
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run.main(["--data-dir", self.dir])
        return code, buffer.getvalue()

    def test_the_dashboard_starts_even_when_the_first_poll_fails(self):
        code, output = self._main(ExplodingDashboard)
        self.assertEqual(code, 0)
        self.assertIn("first poll failed", output)
        self.assertIn("a shape nobody has seen before", output)

    def test_a_healthy_start_says_nothing_about_failures(self):
        code, output = self._main(WorkingDashboard)
        self.assertEqual(code, 0)
        self.assertNotIn("first poll failed", output)


if __name__ == "__main__":
    unittest.main()
