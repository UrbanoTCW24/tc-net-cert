"""Run approved root helper commands from the web panel."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tc_net_cert.web.console import ConsoleBusy, _ASKPASS, _HELPER, _SUDO, _ZENITY, _passwordless


def run_helper(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    if not (_passwordless() or (_SUDO.is_file() and _ZENITY.is_file() and _ASKPASS.is_file())):
        raise ConsoleBusy("Este panel no puede pedir permisos de administrador.")
    argv = [str(_HELPER), *args]
    if _passwordless():
        argv = [str(_SUDO), "-n", "--", *argv]
    else:
        argv = [str(_SUDO), "-A", "--", *argv]
    env = os.environ.copy()
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if _ASKPASS.is_file():
        env["SUDO_ASKPASS"] = str(_ASKPASS)
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConsoleBusy(f"No se pudo ejecutar la acción: {exc}") from exc
