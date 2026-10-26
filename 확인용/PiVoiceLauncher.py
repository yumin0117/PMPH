#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launch standalone voice venv from the current PC/mobile robot server."""
import atexit
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

_VOICE_PROCESS = None


def voice_request(action):
    if action not in ("status", "start", "stop", "ping"):
        return {"state": "error", "error": "invalid action"}
    method = "GET" if action == "status" else "POST"
    try:
        req = Request(f"http://127.0.0.1:5056/{action}", method=method)
        with urlopen(req, timeout=1.5) as response:
            return json.loads(response.read(20000).decode("utf-8"))
    except Exception as exc:
        return {"state": "offline", "text": "", "elapsed": None,
                "message": "음성 서비스 연결 실패", "error": str(exc)}


def _cleanup():
    global _VOICE_PROCESS
    if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
        _VOICE_PROCESS.terminate()
        try:
            _VOICE_PROCESS.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _VOICE_PROCESS.kill()


def start_voice():
    global _VOICE_PROCESS
    if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
        return
    voice_py = Path("/home/pi/picar-voice/bin/python")
    voice_file = Path(__file__).resolve().parent / "PiVoice.py"
    if not voice_py.is_file() or not voice_file.is_file():
        print("[Voice] 음성 환경이 없어 비활성화되었습니다. 다른 기능은 그대로 실행합니다.", flush=True)
        return
    if voice_request("status").get("state") not in ("offline", None):
        print("[Voice] 이미 실행된 서비스가 있어 새로 시작하지 않습니다.", flush=True)
        return
    env = dict(os.environ)
    env["HOME"] = "/home/pi"
    env["OMP_NUM_THREADS"] = "2"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    try:
        command = [str(voice_py), "-u", str(voice_file)]
        if os.geteuid() == 0:
            command = ["runuser", "-u", "pi", "--"] + command
        _VOICE_PROCESS = subprocess.Popen(
            command, cwd=str(voice_file.parent), env=env,
        )
        atexit.register(_cleanup)
        print(f"[Voice] 독립 음성 프로세스 PID={_VOICE_PROCESS.pid}", flush=True)
    except Exception as exc:
        print("[Voice] 실행 오류:", repr(exc), flush=True)
