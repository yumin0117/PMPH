#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""기존 GUI가 호출하는 start_voice/voice_request를 Sherpa 서비스에 연결."""
import atexit
import json
import os
import signal
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

VOICE_URL = "http://127.0.0.1:5056"
_VOICE_PROCESS = None


def voice_request(action):
    if action not in ("status", "start", "stop", "ping"):
        return {"state": "error", "error": "invalid action"}
    if action != "status":
        current = voice_request("status")
        if current.get("engine") != "sherpa":
            return current
    try:
        req = Request(VOICE_URL + "/" + action,
                      method="GET" if action == "status" else "POST")
        with urlopen(req, timeout=1.5) as response:
            result = json.loads(response.read(20000).decode("utf-8"))
        if result.get("engine") != "sherpa":
            return {"state": "error", "text": "", "error":
                    "5056 포트에 이전 음성 서비스가 실행 중입니다. 종료 후 서버를 다시 실행하세요."}
        return result
    except Exception as exc:
        return {"state": "offline", "text": "", "elapsed": None,
                "message": "Sherpa 음성 서비스 연결 대기", "error": str(exc)}


def _cleanup():
    if _VOICE_PROCESS is None or _VOICE_PROCESS.poll() is not None:
        return
    try:
        os.killpg(_VOICE_PROCESS.pid, signal.SIGTERM)
        _VOICE_PROCESS.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(_VOICE_PROCESS.pid, signal.SIGKILL)
        _VOICE_PROCESS.wait(timeout=2)
    except ProcessLookupError:
        pass


def start_voice():
    global _VOICE_PROCESS
    if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
        return
    status = voice_request("status")
    if status.get("state") != "offline":
        print("[음성] " + (status.get("error") or "이미 실행된 Sherpa 서비스를 사용합니다."), flush=True)
        return
    interpreter = Path("/home/pi/picar-voice/bin/python")
    script = Path(__file__).resolve().parent / "PiVoiceSherpa.py"
    if not interpreter.is_file() or not script.is_file():
        print("[음성] ~/picar-voice 환경과 PiVoiceSherpa.py를 확인하세요.", flush=True)
        return
    command = [str(interpreter), "-u", str(script)]
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = "2"
    env["OPENBLAS_NUM_THREADS"] = "1"
    if os.geteuid() == 0:
        command = ["runuser", "-u", "pi", "--"] + command
    try:
        _VOICE_PROCESS = subprocess.Popen(command, cwd=str(script.parent), env=env,
                                          start_new_session=True)
        atexit.register(_cleanup)
        print(f"[음성] Sherpa 서비스 시작 PID={_VOICE_PROCESS.pid}", flush=True)
    except Exception as exc:
        print("[음성] 실행 오류:", exc, flush=True)
