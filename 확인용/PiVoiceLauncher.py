#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launch standalone voice venv from the current PC/mobile robot server.

저장 경로: ~/Adeept_PiCar-Pro/Server/PiVoiceLauncher.py

기존 기능(그대로 유지)
  * start_voice()    : ~/picar-voice 가상환경으로 음성 프로세스 실행
  * voice_request()  : /status /start /stop /ping 프록시 (모바일 서버가 사용)

추가 기능
  * 음성 엔진 선택   : vosk(기본, PiVoiceVosk.py) / whisper(기존 PiVoice.py)
  * VoiceCommandBridge: 음성 서비스가 검증한 명령을 받아 '로봇 서버 프로세스 안에서'
                        기존 robot_ctrl()로 실행 (서버 코드 수정 없음)
      - GUIServer_mobile.py / GUIServer_mobile_final.py(→ import GUIServer_mobile)
            → forward/backward/left/right/stop_move/grab/loose/grip_stop/light_on/light_off
      - GUIServer_custom.py / GUIServer_custom_base.py (PC)
            → forward/backward/left/right/DS/grab/loose/stop/light_on/light_off (+drive_ping)
  * 기본값 MOCK 모드 : 모터·서보를 움직이지 않고 출력만 함
"""
import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

_VOICE_PROCESS = None
_ATEXIT_DONE = False

# =========================================================
# 음성 설정
# =========================================================
VOICE_URL = "http://127.0.0.1:5056"
VOICE_ENGINE = os.environ.get("PICAR_VOICE_ENGINE", "vosk")   # "vosk" | "whisper"
VOICE_FILES = {"vosk": "PiVoiceVosk.py", "whisper": "PiVoice.py"}

# 첫 테스트는 MOCK(로봇 미동작). 실제 동작: False 로 바꾸거나 PICAR_VOICE_MOCK=0
VOICE_MOCK = os.environ.get("PICAR_VOICE_MOCK", "1") != "0"

# 주행 시간 제한 (녹음 10초 제한과 별개)
DRIVE_DEFAULT_MS = 500
DRIVE_MIN_MS = 150
DRIVE_MAX_MS = 1500
GRIP_MS = 1000                 # 집게 닫기/열기 동작 시간
GRIP_KEEPALIVE_SEC = 0.3       # SmoothGripper 0.9초 워치독 유지용
DRIVE_PING_SEC = 0.25          # PC Final 서버 1.2초 주행 워치독 유지용 (drive_ping)
COMMAND_MAX_AGE_SEC = 1.5      # 음성 서비스에서 받은 뒤 이보다 오래된 명령은 폐기 (정지는 예외)
ALLOWED = ("forward", "backward", "left", "right", "stop",
           "grab", "release", "light_on", "light_off")
NAMES_KO = {"forward": "전진", "backward": "후진", "left": "좌회전", "right": "우회전",
            "stop": "정지", "grab": "집게 닫기", "release": "집게 열기",
            "light_on": "조명 ON", "light_off": "조명 자동"}


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
    _stop_bridge()
    if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
        _VOICE_PROCESS.terminate()
        try:
            _VOICE_PROCESS.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _VOICE_PROCESS.kill()


def start_voice():
    global _VOICE_PROCESS, _ATEXIT_DONE
    if _VOICE_PROCESS is not None and _VOICE_PROCESS.poll() is None:
        return
    voice_py = Path("/home/pi/picar-voice/bin/python")
    engine = VOICE_ENGINE if VOICE_ENGINE in VOICE_FILES else "vosk"
    voice_file = Path(__file__).resolve().parent / VOICE_FILES[engine]
    if not voice_py.is_file() or not voice_file.is_file():
        print("[Voice] 음성 환경이 없어 비활성화되었습니다. 다른 기능은 그대로 실행합니다.", flush=True)
        return
    if engine == "vosk":
        _start_bridge()
    if not _ATEXIT_DONE:
        atexit.register(_cleanup)
        _ATEXIT_DONE = True
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
        print(f"[Voice] 독립 음성 프로세스 PID={_VOICE_PROCESS.pid} · 엔진={engine}", flush=True)
    except Exception as exc:
        print("[Voice] 실행 오류:", repr(exc), flush=True)


# =========================================================
# RobotController 어댑터 (로봇 서버 프로세스 안에서 동작)
# =========================================================
class RobotAdapter:
    """__main__ 모듈(GUIServer_mobile.py / GUIServer_custom.py)의 기존 함수를 그대로 호출."""

    PROFILES = {
        "mobile": {"stop": "stop_move", "grip_stop": "grip_stop", "keepalive": True,
                   "drive_ping": False},
        "pc": {"stop": "DS", "grip_stop": "stop", "keepalive": True, "drive_ping": True},
        "mock": {"stop": "stop_move", "grip_stop": "grip_stop", "keepalive": False,
                 "drive_ping": False},
    }
    # 실행 파일이 래퍼(…_final.py, PC Final)여도 실제 제어 함수가 있는 모듈을 찾는다
    CANDIDATES = ("__main__", "GUIServer_mobile", "GUIServer_custom_base", "GUIServer_custom")

    def __init__(self, mock):
        self.mock = mock
        self.mod = None
        self.kind = "mock" if mock else None
        self.lock = threading.Lock()
        self.token = 0
        self.voice_motion = None
        self.voice_grip = 0

    def _detect(self):
        if self.kind is not None:
            return self.kind
        names = list(self.CANDIDATES) + [n for n in list(sys.modules) if n not in self.CANDIDATES]
        for name in names:
            mod = sys.modules.get(name)
            if mod is None or not callable(getattr(mod, "robot_ctrl", None)):
                continue
            if not hasattr(mod, "current_motion"):
                continue
            if hasattr(mod, "MOBILE_HTML") or hasattr(mod, "grip_controller"):
                self.kind, self.mod = "mobile", mod
            elif hasattr(mod, "arm_servo"):
                self.kind, self.mod = "pc", mod
            if self.kind:
                break
        if self.kind is None:
            raise RuntimeError("로봇 서버 함수(robot_ctrl)를 찾지 못함")
        print(f"[Voice] 로봇 어댑터: {self.kind} ({self.mod.__name__})", flush=True)
        return self.kind

    def _ctrl(self, command):
        if self._detect() == "mock":
            print(f"[Voice][MOCK] robot_ctrl('{command}')", flush=True)
            return
        self.mod.robot_ctrl(command)

    def _p(self, key):
        return self.PROFILES[self._detect()][key]

    def _current_motion(self, default=None):
        if self.mod is None:                 # MOCK: 실제 서버 상태가 없으므로 음성 동작 기준
            return default
        return getattr(self.mod, "current_motion", None)

    def _new_token(self):
        with self.lock:
            self.token += 1
            return self.token

    # ---- 명령 ----
    def stop(self, voice_only=False):
        """정지 = 최우선. voice_only=True 면 음성으로 시작한 동작만 정지."""
        token = self._new_token()          # 대기 중인 자동정지/집게 루프 취소
        motion, grip = self.voice_motion, self.voice_grip
        self.voice_motion, self.voice_grip = None, 0
        if voice_only:
            if motion and self._current_motion(motion) == motion:
                self._ctrl(self._p("stop"))
            if grip:
                self._ctrl(self._p("grip_stop"))
            return token
        self._ctrl(self._p("stop"))
        self._ctrl(self._p("grip_stop"))
        return token

    def drive(self, motion, ms):
        token = self._new_token()
        self._ctrl(motion)
        self.voice_motion = motion
        threading.Thread(target=self._drive_loop, args=(token, motion, ms),
                         daemon=True, name="VoiceDrive").start()

    def _drive_loop(self, token, motion, ms):
        end = time.monotonic() + ms / 1000.0
        ping = self._p("drive_ping")
        while True:
            left = end - time.monotonic()
            if left <= 0 or token != self.token:
                break
            time.sleep(min(left, DRIVE_PING_SEC))
            if (ping and token == self.token and time.monotonic() < end
                    and self._current_motion(motion) == motion):
                try:
                    self._ctrl("drive_ping")
                except Exception:
                    pass
        self._auto_stop(token, motion)

    def _auto_stop(self, token, motion):
        with self.lock:
            if token != self.token:
                return
        try:
            # GUI가 그 사이 다른 동작을 시켰으면 건드리지 않음
            if self._current_motion(motion) == motion:
                self._ctrl(self._p("stop"))
                if self.kind == "mock":
                    print("[Voice][MOCK] 자동 정지", flush=True)
        except Exception as exc:
            print("[Voice] 자동 정지 오류:", repr(exc), flush=True)
        finally:
            if self.voice_motion == motion:
                self.voice_motion = None

    def grip(self, direction):
        token = self._new_token()
        cmd = "grab" if direction < 0 else "loose"
        self._ctrl(cmd)
        self.voice_grip = direction
        threading.Thread(target=self._grip_loop, args=(token, cmd, direction),
                         daemon=True, name="VoiceGrip").start()

    def _grip_loop(self, token, cmd, direction):
        end = time.monotonic() + GRIP_MS / 1000.0
        keep = self._p("keepalive")
        try:
            while time.monotonic() < end:
                time.sleep(GRIP_KEEPALIVE_SEC if keep else max(0.0, min(0.1, end - time.monotonic())))
                if token != self.token:
                    return
                if keep and time.monotonic() < end:
                    self._ctrl(cmd)                   # 반복 hold → 마감시간만 연장
            if token != self.token:
                return
            gc = getattr(self.mod, "grip_controller", None) if self.mod else None
            if gc is not None and getattr(gc, "direction", direction) != direction:
                return                                 # GUI가 다른 방향으로 조작 중
            self._ctrl(self._p("grip_stop"))
        except Exception as exc:
            print("[Voice] 집게 정지 오류:", repr(exc), flush=True)
        finally:
            if self.voice_grip == direction and token == self.token:
                self.voice_grip = 0

    def light(self, on):
        self._ctrl("light_on" if on else "light_off")

    def cancel_all(self):
        self._new_token()


# =========================================================
# VoiceCommandBridge: 음성 서비스 → 검증 → RobotAdapter
# =========================================================
def _validate(item):
    if not isinstance(item, dict):
        return None
    cmd = item.get("command")
    if cmd not in ALLOWED:
        return None
    out = {"command": cmd, "id": item.get("id"), "source": item.get("source"),
           "created": item.get("created")}
    if cmd in ("forward", "backward", "left", "right"):
        try:
            ms = int(item.get("duration_ms", DRIVE_DEFAULT_MS))
        except (TypeError, ValueError):
            ms = DRIVE_DEFAULT_MS
        out["duration_ms"] = max(DRIVE_MIN_MS, min(DRIVE_MAX_MS, ms))
    return out


class VoiceCommandBridge(threading.Thread):
    def __init__(self, mock=VOICE_MOCK):
        super().__init__(daemon=True, name="VoiceCommandBridge")
        self.adapter = RobotAdapter(mock)
        self.stop_event = threading.Event()
        self.last_id = None

    def _get_command(self):
        req = Request(VOICE_URL + "/command?wait=1.0", method="GET")
        with urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read(4000).decode("utf-8")).get("command")

    def _report(self, payload):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(VOICE_URL + "/result", data=data, method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=1.0) as resp:
                resp.read(200)
        except Exception:
            pass

    def run(self):
        print(f"[Voice] 명령 브리지 시작 · {'MOCK(로봇 미동작)' if self.adapter.mock else '실제 제어'}",
              flush=True)
        while not self.stop_event.is_set():
            try:
                item = self._get_command()
            except Exception:
                self.stop_event.wait(1.0)          # 음성 서비스 준비 전/종료 → 재시도
                continue
            if item:
                self._process_item(item)

    def _process_item(self, raw):
        item = _validate(raw)
        if item is None:
            print("[Voice] 잘못된 명령 형식 → 실행 안 함", flush=True)
            return
        if item["id"] is not None and item["id"] == self.last_id:
            return                                  # 중복 실행 방지
        self.last_id = item["id"]
        cmd = item["command"]
        result = {"id": item["id"], "command": cmd, "ok": False, "message": ""}
        created = item.get("created")
        age = time.monotonic() - created if isinstance(created, (int, float)) else 0.0
        if cmd != "stop" and age > COMMAND_MAX_AGE_SEC:
            result["message"] = "오래된 명령 폐기"
            print(f"[Voice] 명령 #{item['id']} 오래됨({age:.1f}s) → 폐기", flush=True)
            self._report(result)
            return
        try:
            result["action_start"] = time.monotonic()
            if cmd == "stop":
                self.adapter.stop(voice_only=(item.get("source") == "safety"))
            elif cmd in ("forward", "backward", "left", "right"):
                self.adapter.drive(cmd, item["duration_ms"])
            elif cmd == "grab":
                self.adapter.grip(-1)
            elif cmd == "release":
                self.adapter.grip(1)
            elif cmd == "light_on":
                self.adapter.light(True)
            elif cmd == "light_off":
                self.adapter.light(False)
            result["done"] = time.monotonic()
            result["ok"] = True
            extra = f" {item['duration_ms']}ms" if "duration_ms" in item else ""
            print(f"[Voice] 명령 #{item['id']} {NAMES_KO[cmd]}{extra} 실행", flush=True)
        except Exception as exc:
            result["message"] = f"실행 오류 {type(exc).__name__}"
            print("[Voice] 명령 실행 오류:", repr(exc), flush=True)
            try:
                self.adapter.stop()
            except Exception:
                pass
        self._report(result)

    def shutdown(self):
        self.stop_event.set()
        self.adapter.cancel_all()      # 서버 종료 처리(safe_stop)와 충돌하지 않게 타이머만 취소


_BRIDGE = None


def _start_bridge():
    global _BRIDGE
    if _BRIDGE is not None and _BRIDGE.is_alive():
        return
    _BRIDGE = VoiceCommandBridge()
    _BRIDGE.start()


def _stop_bridge():
    if _BRIDGE is not None:
        _BRIDGE.shutdown()
