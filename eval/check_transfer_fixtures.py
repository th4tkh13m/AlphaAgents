"""Pre-launch fixture checks with cold-start patience and no shared ADB restart.

This shim is confined to provisioning; benchmark episode settings are unchanged.
"""

import runpy
import sys
import tempfile
from pathlib import Path

from android_env.components import adb_controller

original_execute = adb_controller.AdbController.execute_command


def execute(self, args, timeout=None, device_specific=True):
    return original_execute(
        self, args, timeout=max(timeout or 0, 60), device_specific=device_specific
    )


adb_controller.AdbController.execute_command = execute
adb_controller.AdbController._restart_server = lambda self, timeout=None: None

if __name__ == "__main__":
    temporary = Path(sys.argv[1]).resolve() / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary)
    runpy.run_path(
        str(
            Path(__file__).resolve().parents[1]
            / "runs/experiments/check_full_fixtures.py"
        ),
        run_name="__main__",
    )
