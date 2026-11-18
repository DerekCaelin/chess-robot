# toolrun.py - run a calibration tool as a subprocess with cancel semantics.
#
# Snapshot() captures settings + points.txt before the tool starts. On
# cancel (or crash) we restore that snapshot, which gives the UI its promise:
# "at any point during calibration, cancel reverts to the prior state".
# On clean exit we keep whatever the tool saved.
#
# Output lines are streamed through callbacks; the UI marshals them onto the
# Tk main loop itself.

import os
import re
import subprocess
import threading

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chessapp import settings  # noqa: E402

FIT_RE = re.compile(r"AL5D_(radscale|stickout|gfudge)\s*=\s*(-?[\d.]+)")


def parse_fit(text):
    """Extract suggested constants from arm_probe --fit output."""
    found = {}
    for m in FIT_RE.finditer(text):
        found["arm." + m.group(1)] = float(m.group(2))
    return found


class ToolRun:
    def __init__(self, script, args=(), name=""):
        self.script = script
        self.args = list(args)
        self.name = name or os.path.basename(script)
        self._snap = None
        self._proc = None
        self._thread = None
        self.cancelled = False

    def start(self, on_line, on_done):
        self._snap = settings.snapshot()
        cmd = [sys.executable, os.path.join(ROOT, self.script)] + self.args
        self._proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)

        def pump():
            try:
                for line in self._proc.stdout:
                    on_line(line.rstrip())
            except Exception:
                pass
            rc = self._proc.wait()
            if self.cancelled:
                settings.restore(self._snap)
            elif rc != 0:
                settings.restore(self._snap)
            on_done(rc, self.cancelled)
        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def cancel(self):
        self.cancelled = True
        try:
            self._proc.terminate()
        except Exception:
            pass
