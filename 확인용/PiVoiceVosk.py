#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HazardScout-AI 음성 서비스 (Vosk STT + Claude 명령 분석)

저장 경로 : ~/Adeept_PiCar-Pro/Server/PiVoiceVosk.py
실행 환경 : ~/picar-voice 가상환경 (PiVoiceLauncher.py가 자동 실행)

동작 순서
  ReSpeaker Lite → (RAM 버퍼) → Vosk 한국어 인식 → Claude API(JSON) → 검증
  → localhost:5056/command 로 대기 → 로봇 서버(PiVoiceLauncher 브리지)가 가져가서 실행

규칙
  * 음성 파일(WAV/MP3/PCM/임시파일)을 절대 만들지 않는다. 오디오는 RAM 큐에만 존재.
  * 인식 문장은 화면/터미널/로그/파일/GUI 어디에도 출력하지 않는다.
    (Claude API로 보낼 때만 메모리에 잠깐 보관 후 즉시 참조 해제)
  * "멈춰/정지/스톱"은 Claude를 기다리지 않고 즉시 정지 명령으로 처리한다.
  * 기존 PiVoice.py(Whisper)와 같은 포트·같은 API(/status /start /stop /ping)를 써서
    기존 PC·모바일 GUI를 수정하지 않고 그대로 사용한다.

단독 테스트(메뉴) :  python PiVoiceVosk.py --menu
서비스 모드       :  python PiVoiceVosk.py
"""

import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np

# =========================================================
# Claude API 키
#   아래 따옴표 안에 API 키를 넣으세요. (예: "sk-ant-api03-....")
#   그대로 두면 환경변수 ANTHROPIC_API_KEY 를 사용합니다.
#   ⚠ 키를 넣은 상태로 GitHub에 올리지 마세요.
# =========================================================
ANTHROPIC_API_KEY = "api키 넣을 곳"

# =========================================================
# 설정 상수
# =========================================================
HOST = "127.0.0.1"          # 외부 공개 금지 (로봇 서버만 접근)
PORT = 5056                 # 기존 PiVoice.py와 동일 → 기존 GUI 프록시 그대로 사용

# --- Vosk 모델 ---
VOSK_MODEL_PATH = os.environ.get(
    "PICAR_VOSK_MODEL",
    str(Path(__file__).resolve().parent / "models" / "vosk-model-small-ko-0.22"),
)
MODEL_LABEL = "vosk-small-ko-0.22"
SAMPLE_RATE = 16000         # Vosk 한국어 Small 모델 입력 샘플레이트
USE_COMMAND_GRAMMAR = False  # True면 명령 단어로 어휘 제한(정확도↑, 자유 문장↓)
COMMAND_GRAMMAR = [
    "앞으로", "가", "전진", "전진해", "뒤로", "후진", "후진해", "왼쪽으로", "오른쪽으로",
    "왼쪽", "오른쪽", "멈춰", "정지", "스톱", "스탑", "그만", "집게로", "집게", "잡아",
    "놓아", "놔", "불", "켜", "꺼", "라이트", "조금", "많이", "[unk]",
]

# --- 마이크 ---
MIC_NAME_KEYWORDS = ("respeaker", "re speaker")
ALLOW_FALLBACK_MIC = os.environ.get("PICAR_VOICE_ANY_MIC", "0") == "1"  # PC 테스트용
CHUNK_SECONDS = 0.05        # 50ms 단위로 순차 처리
INPUT_GAIN = 1.0            # 입력이 작으면 1.5~2.0
AUDIO_QUEUE_MAX = 400       # RAM 버퍼 상한 (50ms × 400 = 20초 분량, 그 이상은 버림)

# --- 녹음 길이 / 발화 종료 감지 ---
MAX_RECORD_SECONDS = 10.0   # 한 번의 음성 입력 최대 10초 (필수)
USE_VAD_END = True          # 무음 감지로 빠른 종료
END_SILENCE_SECONDS = 0.9   # 말 끝난 뒤 이 시간만큼 조용하면 종료 (잘리면 1.2~1.5로)
MIN_SPEECH_SECONDS = 0.30   # 이보다 짧은 소리는 잡음으로 보고 VAD 종료 안 함
NO_SPEECH_TIMEOUT = 5.0     # 버튼 누르고 이 시간 동안 말이 없으면 종료
SPEECH_RMS_MIN = 0.010      # 음성 판정 최소 RMS (0~1)
SPEECH_NOISE_RATIO = 3.0    # 배경잡음 대비 배율
USE_VOSK_ENDPOINT = False   # True면 Vosk 자체 끝점 검출로도 종료
HEARTBEAT_TIMEOUT = 3.0     # GUI ping 끊기면 녹음 자동 종료

# --- 긴급 정지 (Claude 없이 즉시) ---
STOP_KEYWORDS = ("멈춰", "멈춰라", "멈추", "정지", "스톱", "스탑", "그만")

# --- Claude API ---
CLAUDE_MODEL = os.environ.get("PICAR_CLAUDE_MODEL", "claude-haiku-4-5")
CLAUDE_TIMEOUT_SEC = 4.0
CLAUDE_MAX_TOKENS = 40
MAX_REQUESTS_PER_MINUTE = 12
MAX_REQUESTS_PER_DAY = 300
USAGE_FILE = Path.home() / ".picar_voice_usage.json"   # 날짜·횟수만 저장 (문장 저장 안 함)
COMMAND_MAX_DELAY_SEC = 6.0  # 발화 종료 후 이 시간 넘으면 오래된 명령으로 폐기

# --- 명령 검증 (서버에서 다시 한 번 검증함) ---
ALLOWED_COMMANDS = ("forward", "backward", "left", "right", "stop",
                    "grab", "release", "light_on", "light_off", "none")
DRIVE_COMMANDS = ("forward", "backward", "left", "right")
DRIVE_DEFAULT_MS = 500
DRIVE_MIN_MS = 150
DRIVE_MAX_MS = 1500

# --- 자원 ---
CPU_AFFINITY = None          # 예: {3} → 음성 처리를 CPU 3번 코어에만 고정
STATS_KEEP = 200

COMMAND_NAMES_KO = {
    "forward": "전진", "backward": "후진", "left": "좌회전", "right": "우회전",
    "stop": "정지", "grab": "집게 닫기", "release": "집게 열기",
    "light_on": "조명 ON", "light_off": "조명 자동", "none": "없음",
}

SYSTEM_PROMPT = (
    "너는 바퀴 로봇의 한국어 음성 명령 분류기다. 입력은 음성 인식 결과라 띄어쓰기나 "
    "철자가 틀릴 수 있다. 아래 명령 중 하나만 골라 JSON 한 줄만 출력한다. 설명 금지.\n"
    "forward=앞으로/전진, backward=뒤로/후진, left=왼쪽, right=오른쪽, stop=멈춤/정지, "
    "grab=집게로 잡기/닫기, release=집게 놓기/열기, light_on=불·라이트 켜기, "
    "light_off=불·라이트 끄기, none=모호하거나 해당 없음.\n"
    "forward/backward/left/right에만 duration_ms(정수 200~1500, 기본 500, '조금'=300, "
    "'많이'·'길게'=1200)를 넣는다.\n"
    '예: {"command":"forward","duration_ms":500} {"command":"light_on"} {"command":"none"}'
)


# =========================================================
# helper
# =========================================================
def log(msg):
    """상태 출력 전용. 인식 문장은 절대 넘기지 말 것."""
    print(f"[Voice] {msg}", flush=True)


def rss_mb():
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return None


def normalize(text):
    return re.sub(r"\s+", "", text or "")


def has_stop_keyword(text):
    t = normalize(text)
    return bool(t) and any(k in t for k in STOP_KEYWORDS)


def has_hangul(text):
    return bool(re.search(r"[가-힣]", text or ""))


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def resolve_api_key():
    key = (ANTHROPIC_API_KEY or "").strip()
    if not key or key == "api키 넣을 곳":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return key or None


# =========================================================
# SpeechRecognizer 인터페이스 (추후 CloudRecognizer로 교체 가능)
# =========================================================
class SpeechRecognizer:
    name = "base"
    sample_rate = SAMPLE_RATE

    def load(self):
        raise NotImplementedError

    def new_session(self):
        """발화 1회용 세션 객체 반환 (feed / partial / finish 제공)."""
        raise NotImplementedError


class VoskSession:
    def __init__(self, rec):
        self.rec = rec
        self.segments = []

    def feed(self, pcm16_bytes):
        """True = Vosk가 문장 끝점을 검출함"""
        if self.rec.AcceptWaveform(pcm16_bytes):
            seg = json.loads(self.rec.Result()).get("text", "")
            if seg:
                self.segments.append(seg)
            return True
        return False

    def partial(self):
        parts = list(self.segments)
        parts.append(json.loads(self.rec.PartialResult()).get("partial", ""))
        return " ".join(p for p in parts if p)

    def has_text(self):
        return bool(self.segments)

    def finish(self):
        last = json.loads(self.rec.FinalResult()).get("text", "")
        if last:
            self.segments.append(last)
        text = " ".join(self.segments).strip()
        self.segments = []
        self.rec = None
        return text


class VoskRecognizer(SpeechRecognizer):
    name = "vosk"

    def __init__(self, model_path=VOSK_MODEL_PATH):
        self.model_path = model_path
        self.model = None
        self._vosk = None

    def load(self):
        import vosk
        vosk.SetLogLevel(-1)          # Vosk 내부 로그(인식 문장 포함 가능) 끄기
        if not Path(self.model_path).is_dir():
            raise FileNotFoundError(f"Vosk 모델 폴더가 없습니다: {self.model_path}")
        self._vosk = vosk
        self.model = vosk.Model(self.model_path)   # 프로그램 시작 시 1회만 로드

    def new_session(self):
        if USE_COMMAND_GRAMMAR:
            rec = self._vosk.KaldiRecognizer(
                self.model, SAMPLE_RATE, json.dumps(COMMAND_GRAMMAR, ensure_ascii=False))
        else:
            rec = self._vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.SetWords(False)
        return VoskSession(rec)


# =========================================================
# 마이크 입력 (스트림 1회 열고 재사용, RAM 큐만 사용)
# =========================================================
class MicInput:
    def __init__(self):
        self.sd = None
        self.stream = None
        self.rate = SAMPLE_RATE
        self.name = "미확인"
        self.capturing = False
        self.q = queue.Queue(maxsize=AUDIO_QUEUE_MAX)
        self.dropped = 0
        self._resample = None

    def list_devices(self):
        import sounddevice as sd
        out = []
        for idx, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0:
                out.append((idx, str(info["name"]), int(info["max_input_channels"]),
                            int(info["default_samplerate"])))
        return out

    def _find(self, sd):
        devices = sd.query_devices()
        for idx, info in enumerate(devices):
            name = str(info["name"]).lower()
            if info["max_input_channels"] > 0 and any(k in name for k in MIC_NAME_KEYWORDS):
                return idx, info
        if ALLOW_FALLBACK_MIC:
            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                log("ReSpeaker를 찾지 못해 기본 마이크를 사용합니다 (테스트 모드)")
                return idx, sd.query_devices(idx)
        names = [str(d["name"]) for d in devices if d["max_input_channels"] > 0]
        raise RuntimeError("ReSpeaker Lite 입력 장치가 없습니다. arecord -l 확인. 검색된 입력: "
                           + ", ".join(names))

    def _callback(self, indata, frames, timing, status):
        # PortAudio 콜백: 복사만 한다. 인식은 절대 여기서 하지 않음.
        if not self.capturing:
            return
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.dropped += 1

    def open(self):
        if self.stream is not None:
            return
        import sounddevice as sd
        self.sd = sd
        device, info = self._find(sd)
        self._resample = None
        channels = 2 if int(info["max_input_channels"]) >= 2 else 1
        rates = [SAMPLE_RATE]
        native = int(info["default_samplerate"])
        if native not in rates:
            rates.append(native)
        last_exc = None
        for rate in rates:
            try:
                sd.check_input_settings(device=device, channels=channels,
                                        samplerate=rate, dtype="int16")
                stream = sd.InputStream(device=device, channels=channels, samplerate=rate,
                                        dtype="int16", blocksize=int(rate * CHUNK_SECONDS),
                                        callback=self._callback)
                stream.start()
                self.stream, self.rate, self.name = stream, rate, str(info["name"])
                if rate != SAMPLE_RATE:
                    from math import gcd
                    from scipy.signal import resample_poly
                    g = gcd(rate, SAMPLE_RATE)
                    up, down = SAMPLE_RATE // g, rate // g
                    self._resample = lambda x: resample_poly(x, up, down)
                log(f"마이크 연결: {self.name} · {rate}Hz · {channels}ch")
                return
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"마이크 샘플레이트 연결 실패: {last_exc}")

    def set_capture(self, on):
        if on:
            self.drain()
            self.dropped = 0
        self.capturing = on

    def drain(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def to_pcm16(self, chunk):
        """int16 ndarray → (16kHz int16 bytes, rms 0~1)"""
        x = chunk.astype(np.float32) / 32768.0
        if self._resample is not None:
            x = self._resample(x)
        if INPUT_GAIN != 1.0:
            x = np.clip(x * INPUT_GAIN, -1.0, 1.0)
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        pcm = (x * 32767.0).astype(np.int16)
        return pcm.tobytes(), rms, int(pcm.size)

    def close(self):
        self.capturing = False
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        self.drain()


# =========================================================
# 명령 검증
# =========================================================
class RobotCommandValidator:
    @staticmethod
    def validate(obj):
        """허용 목록 밖이거나 형식이 틀리면 None. duration_ms는 서버 기준으로 제한."""
        if not isinstance(obj, dict):
            return None
        cmd = obj.get("command")
        if not isinstance(cmd, str) or cmd not in ALLOWED_COMMANDS:
            return None
        out = {"command": cmd}
        if cmd in DRIVE_COMMANDS:
            ms = obj.get("duration_ms", DRIVE_DEFAULT_MS)
            try:
                ms = int(ms)
            except (TypeError, ValueError):
                ms = DRIVE_DEFAULT_MS
            out["duration_ms"] = max(DRIVE_MIN_MS, min(DRIVE_MAX_MS, ms))
        return out


def parse_json_command(raw):
    """Claude 응답에서 JSON 객체 1개만 추출. 실패 시 None."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# =========================================================
# 사용량 제한 (분당·일일)
# =========================================================
class UsageLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.minute = deque()
        self.day = date.today().isoformat()
        self.count = 0
        try:
            data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if data.get("date") == self.day:
                self.count = int(data.get("count", 0))
        except Exception:
            pass

    def allow(self):
        with self.lock:
            now = time.monotonic()
            today = date.today().isoformat()
            if today != self.day:
                self.day, self.count = today, 0
            while self.minute and now - self.minute[0] > 60:
                self.minute.popleft()
            if len(self.minute) >= MAX_REQUESTS_PER_MINUTE:
                return False, "분당 요청 한도 초과"
            if self.count >= MAX_REQUESTS_PER_DAY:
                return False, "일일 요청 한도 초과"
            self.minute.append(now)
            self.count += 1
            try:
                USAGE_FILE.write_text(json.dumps({"date": self.day, "count": self.count}),
                                      encoding="utf-8")
            except Exception:
                pass
            return True, ""

    def summary(self):
        with self.lock:
            return {"today": self.count, "day_limit": MAX_REQUESTS_PER_DAY,
                    "minute_limit": MAX_REQUESTS_PER_MINUTE}


# =========================================================
# Claude 명령 분석
# =========================================================
class ClaudeCommandAnalyzer:
    def __init__(self):
        self.client = None
        self.error = ""
        key = resolve_api_key()
        if not key:
            self.error = "Claude API 키 없음 (PiVoiceVosk.py 상단 또는 ANTHROPIC_API_KEY)"
            return
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=key, timeout=CLAUDE_TIMEOUT_SEC,
                                              max_retries=0)
        except Exception as exc:
            self.error = f"anthropic SDK 초기화 실패: {type(exc).__name__}"

    def analyze(self, text):
        """반환: (dict|None, 오류문자열). 응답 원문/입력 문장은 로그에 남기지 않는다."""
        if self.client is None:
            return None, self.error
        try:
            resp = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:
            name = type(exc).__name__
            if "Timeout" in name:
                return None, "Claude API 응답 시간 초과"
            status = getattr(exc, "status_code", None)
            return None, f"Claude API 오류: {name}" + (f" ({status})" if status else "")
        raw = "".join(getattr(b, "text", "") for b in resp.content
                      if getattr(b, "type", "") == "text")
        obj = parse_json_command(raw)
        if obj is None:
            return None, "Claude 응답 JSON 형식 오류"
        try:
            u = resp.usage
            log(f"Claude 토큰 in={u.input_tokens} out={u.output_tokens}")
        except Exception:
            pass
        return obj, ""


# =========================================================
# 지연 통계
# =========================================================
class LatencyStats:
    KEYS = ("1_발화종료→인식완료", "2_인식완료→Claude응답", "3_Claude응답→서버처리완료",
            "4_발화종료→로봇동작시작")

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {k: deque(maxlen=STATS_KEEP) for k in self.KEYS}

    def add(self, key, sec):
        if sec is None or sec < 0:
            return
        with self.lock:
            self.data[key].append(sec)

    def summary(self):
        with self.lock:
            out = {}
            for k, v in self.data.items():
                vals = list(v)
                if vals:
                    out[k] = {"n": len(vals), "avg": round(sum(vals) / len(vals), 3),
                              "max": round(max(vals), 3), "p95": round(percentile(vals, 95), 3)}
                else:
                    out[k] = {"n": 0}
            return out

    def print_summary(self):
        for k, v in self.summary().items():
            if v.get("n"):
                log(f"  {k}: 평균 {v['avg']}s · 최대 {v['max']}s · P95 {v['p95']}s (n={v['n']})")


# =========================================================
# 음성 서비스 본체
# =========================================================
class VoiceService:
    def __init__(self, recognizer=None, use_heartbeat=True):
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.recognizer = recognizer or VoskRecognizer()
        self.mic = MicInput()
        self.validator = RobotCommandValidator()
        self.analyzer = None
        self.limiter = UsageLimiter()
        self.stats = LatencyStats()
        self.use_heartbeat = use_heartbeat

        self.state = "loading"
        self.message = "한국어 인식 모델 준비 중"
        self.error = ""
        self.result_text = ""        # GUI에 보이는 값: 명령 결과 요약만 (인식 문장 아님)
        self.elapsed = None
        self.utt_id = 0
        self.stop_event = threading.Event()
        self.last_heartbeat = 0.0
        self.last_bridge_seen = 0.0

        self.cmd_cond = threading.Condition()
        self.pending = None
        self.cmd_seq = 0
        self.inflight = {}           # id → 타이밍 (결과 보고 대기)

        self.analysis_q = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self._next_session = None    # 다음 발화용 인식기 미리 생성 (시작 지연 감소)
        self._session_lock = threading.Lock()
        self.stt_only = False        # 메뉴 2번: Claude 호출 없이 인식만 확인

        threading.Thread(target=self._load, daemon=True, name="VoiceLoad").start()
        threading.Thread(target=self._analysis_worker, daemon=True, name="ClaudeWorker").start()
        if use_heartbeat:
            threading.Thread(target=self._watchdog, daemon=True, name="VoiceWatchdog").start()

    # ---------- 초기화 ----------
    def _load(self):
        try:
            if CPU_AFFINITY and hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, CPU_AFFINITY)
            t0 = time.monotonic()
            self.recognizer.load()
            log(f"Vosk 모델 로드 {time.monotonic() - t0:.1f}s · RSS {rss_mb()}MB")
            self._prepare_session()
            self.analyzer = ClaudeCommandAnalyzer()
            if self.analyzer.error:
                log("⚠ " + self.analyzer.error + " → 긴급 정지만 동작")
            else:
                log(f"Claude 모델: {CLAUDE_MODEL} · 시간 제한 {CLAUDE_TIMEOUT_SEC}s")
            try:
                self.mic.open()
            except Exception as exc:
                log(f"마이크 대기: {exc}")
            with self.lock:
                self.state = "ready"
                self.message = "음성 인식 준비 완료"
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.error = f"모델 준비 실패: {type(exc).__name__}: {exc}"
                self.message = self.error
            log(self.error)

    # ---------- 인식기 세션 ----------
    def _prepare_session(self):
        try:
            sess = self.recognizer.new_session()
        except Exception as exc:
            log(f"인식기 준비 실패: {type(exc).__name__}")
            return
        with self._session_lock:
            self._next_session = sess

    def _take_session(self):
        with self._session_lock:
            sess, self._next_session = self._next_session, None
        return sess or self.recognizer.new_session()

    # ---------- 녹음 시작/종료 ----------
    def start(self):
        with self.lock:
            if self.state not in ("ready", "done", "error") or self.recognizer.model is None:
                return self.status()
            if self.analysis_q.full():
                return self.status()
            self.state = "opening"
            self.message = "ReSpeaker Lite 연결 중"
            self.error = ""
        try:
            self.mic.open()
            session = self._take_session()
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.error = f"마이크 열기 실패: {type(exc).__name__}: {exc}"
                self.message = self.error
            log(self.error)
            return self.status()
        with self.lock:
            self.utt_id += 1
            uid = self.utt_id
            self.stop_event.clear()
            self.last_heartbeat = time.monotonic()
            self.state = "recording"
            self.message = "녹음 중 · 버튼을 놓으면 분석 (최대 10초)"
            self.elapsed = None
        self.mic.set_capture(True)
        threading.Thread(target=self._utterance_worker, args=(uid, session),
                         daemon=True, name=f"Utterance{uid}").start()
        return self.status()

    def stop(self):
        with self.lock:
            if self.state in ("recording", "opening"):
                self.stop_event.set()
        return self.status()

    def ping(self):
        with self.lock:
            if self.state in ("opening", "recording"):
                self.last_heartbeat = time.monotonic()
        return self.status()

    def _watchdog(self):
        while not self.closed.wait(0.5):
            with self.lock:
                expired = (self.state == "recording" and
                           time.monotonic() - self.last_heartbeat > HEARTBEAT_TIMEOUT)
            if expired:
                log("GUI 연결 끊김 · 녹음 자동 종료")
                self.stop_event.set()

    # ---------- 발화 1회 처리 ----------
    def _utterance_worker(self, uid, session):
        t_open = time.monotonic()
        deadline = t_open + MAX_RECORD_SECONDS
        max_samples = int(MAX_RECORD_SECONDS * SAMPLE_RATE)
        samples = 0
        chunks_seen = 0
        noise = None
        speech_t0 = None
        last_voice = None
        reason = None
        cpu0 = time.process_time()
        try:
            while True:
                now = time.monotonic()
                if self.stop_event.is_set():
                    reason = "버튼 놓음"
                    break
                if now >= deadline or samples >= max_samples:
                    reason = "10초 제한"
                    break
                if speech_t0 is None and now - t_open >= NO_SPEECH_TIMEOUT:
                    reason = "말소리 없음"
                    break
                if (USE_VAD_END and speech_t0 is not None and last_voice is not None
                        and last_voice - speech_t0 >= MIN_SPEECH_SECONDS
                        and now - last_voice >= END_SILENCE_SECONDS):
                    reason = "무음 감지"
                    break
                try:
                    chunk = self.mic.q.get(timeout=0.03)
                except queue.Empty:
                    if chunks_seen == 0 and time.monotonic() - t_open > 1.5:
                        reason = "오류 마이크 무응답"
                        break
                    continue
                chunks_seen += 1
                pcm, rms, n = self.mic.to_pcm16(chunk)
                del chunk
                remain = max_samples - samples
                if n > remain:                       # 10초 초과분은 현재 명령에 포함 안 함
                    pcm, n = pcm[:remain * 2], remain
                samples += n
                # 간단 에너지 VAD (배경잡음 자동 추정)
                threshold = max(SPEECH_RMS_MIN, (noise or 0.0) * SPEECH_NOISE_RATIO)
                if rms >= threshold:
                    if speech_t0 is None:
                        speech_t0 = now
                    last_voice = now
                elif speech_t0 is None:
                    noise = rms if noise is None else noise * 0.9 + rms * 0.1
                endpoint = session.feed(pcm)
                del pcm
                if has_stop_keyword(session.partial()):
                    reason = "긴급 정지"
                    break
                if USE_VOSK_ENDPOINT and endpoint and session.has_text():
                    reason = "Vosk 끝점"
                    break
        except Exception as exc:
            reason = f"오류 {type(exc).__name__}"
        finally:
            self.mic.set_capture(False)
            self.mic.drain()                         # 남은 오디오 버퍼 즉시 폐기
            if chunks_seen == 0:
                log("마이크 입력 없음 → 다음 녹음 때 다시 연결")
                try:
                    self.mic.close()
                except Exception:
                    pass
            threading.Thread(target=self._prepare_session, daemon=True).start()

        t_end = time.monotonic()
        t_speech_end = last_voice if (reason == "무음 감지" and last_voice) else t_end
        dur = samples / SAMPLE_RATE
        try:
            text = session.finish()
        except Exception:
            text = ""
        session = None
        t_stt = time.monotonic()
        stt_cpu = time.process_time() - cpu0
        log(f"#{uid} 입력 종료({reason}) · 길이 {dur:.1f}s · 인식 {t_stt - t_end:.2f}s · "
            f"CPU {stt_cpu:.2f}s · RSS {rss_mb()}MB"
            + (f" · 버퍼 초과 {self.mic.dropped}" if self.mic.dropped else ""))

        timing = {"t_speech_end": t_speech_end, "t_stt": t_stt}
        self.stats.add(LatencyStats.KEYS[0], t_stt - t_speech_end)

        # 긴급 정지: Claude 대기 없이 바로 전달
        if reason == "긴급 정지" or has_stop_keyword(text):
            del text
            self._push_command({"command": "stop"}, timing, source="emergency")
            with self.lock:
                self.state = "done"
                self.message = "긴급 정지 명령 전달"
            return

        if (not text or not has_hangul(text) or speech_t0 is None
                or reason.startswith("오류")):
            del text
            if reason.startswith("오류"):
                self._finish("⚠ 마이크 입력 오류", ok=False, error="마이크 입력 없음 · USB 연결 확인")
            else:
                self._finish(f"❓ 음성을 인식하지 못함 [#{uid}]", ok=False)
            return

        if self.stt_only:
            n = len(normalize(text))
            del text
            self._finish(f"✅ 인식 성공 ({n}글자) [#{uid}]", ok=True)
            return

        with self.lock:
            self.state = "processing"
            self.message = "Claude 명령 분석 중"
        try:
            self.analysis_q.put_nowait((uid, text, timing))
        except queue.Full:
            self._finish("⚠ 이전 명령 분석 중", ok=False)
        del text

    # ---------- Claude 분석 (별도 스레드) ----------
    def _analysis_worker(self):
        while not self.closed.is_set():
            try:
                uid, text, timing = self.analysis_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._analyze_one(uid, text, timing)
            except Exception as exc:
                log(f"#{uid} 분석 예외 {type(exc).__name__} → 명령 실행 안 함")
                self._finish("⚠ 분석 오류", ok=False, error=f"분석 오류 {type(exc).__name__}")
            finally:
                text = None                          # 인식 문장 참조 해제

    def _analyze_one(self, uid, text, timing):
        ok, why = self.limiter.allow()
        if not ok:
            self._finish(f"⚠ {why}", ok=False)
            return
        if self.analyzer is None:
            self._finish("⚠ Claude 준비 안 됨", ok=False)
            return
        obj, err = self.analyzer.analyze(text)
        t_claude = time.monotonic()
        timing["t_claude"] = t_claude
        self.stats.add(LatencyStats.KEYS[1], t_claude - timing["t_stt"])
        if err:
            log(f"#{uid} {err} → 안전 정지")
            self._push_command({"command": "stop"}, timing, source="safety")
            self._finish(f"⚠ {err}", ok=False, error=err)
            return
        if t_claude - timing["t_speech_end"] > COMMAND_MAX_DELAY_SEC:
            self._finish("⚠ 응답 지연으로 명령 폐기", ok=False)
            return
        cmd = self.validator.validate(obj)
        if cmd is None:
            log(f"#{uid} 허용되지 않은 명령 → 실행 안 함")
            self._finish("⚠ 허용되지 않은 명령", ok=False)
            return
        if cmd["command"] == "none":
            self._finish(f"❓ 명령을 이해하지 못함 [#{uid}]", ok=False)
            return
        log(f"#{uid} Claude {t_claude - timing['t_stt']:.2f}s → {json.dumps(cmd)}")
        self._push_command(cmd, timing, source="claude")
        with self.lock:
            self.state = "done"
            self.message = "명령 전달"

    def _finish(self, summary, ok, error=""):
        with self.lock:
            self.result_text = summary
            self.state = "error" if error else "done"
            self.error = error
            self.message = "완료" if ok else "명령 없음"

    # ---------- 로봇 서버로 명령 전달 (long-poll) ----------
    def _push_command(self, cmd, timing, source):
        with self.cmd_cond:
            self.cmd_seq += 1
            item = dict(cmd)
            item.update({"id": self.cmd_seq, "source": source,
                         "created": time.monotonic()})
            # 정지는 항상 최우선: 대기 중인 다른 명령을 덮어씀
            if self.pending is None or cmd["command"] == "stop" or \
                    self.pending.get("command") != "stop":
                self.pending = item
            self.inflight[self.cmd_seq] = dict(timing, command=cmd["command"], source=source)
            while len(self.inflight) > 20:
                self.inflight.pop(next(iter(self.inflight)))
            self.cmd_cond.notify_all()
        name = COMMAND_NAMES_KO.get(cmd["command"], cmd["command"])
        with self.lock:
            if source != "safety":
                self.result_text = f"⏳ {name} 전달 중 [#{self.cmd_seq}]"

    def take_command(self, wait=1.0):
        self.last_bridge_seen = time.monotonic()
        end = time.monotonic() + max(0.0, min(wait, 5.0))
        with self.cmd_cond:
            while self.pending is None:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cmd_cond.wait(left)
            item, self.pending = self.pending, None
        return item

    def report_result(self, data):
        try:
            cid = int(data.get("id"))
        except Exception:
            return {"ok": False}
        timing = self.inflight.pop(cid, None)
        ok = bool(data.get("ok"))
        name = COMMAND_NAMES_KO.get(str(data.get("command")), str(data.get("command")))
        msg = str(data.get("message", ""))[:60]
        act = data.get("action_start")
        done = data.get("done")
        total = None
        if timing and isinstance(act, (int, float)):
            total = act - timing["t_speech_end"]
            self.stats.add(LatencyStats.KEYS[3], total)
            if timing.get("t_claude") and isinstance(done, (int, float)):
                self.stats.add(LatencyStats.KEYS[2], done - timing["t_claude"])
        if timing and timing.get("source") == "safety":
            log(f"안전 정지 #{cid} 처리: {msg or 'ok'}")
            return {"ok": True}
        with self.lock:
            mark = "✅" if ok else "⚠"
            self.result_text = f"{mark} {name} {'실행' if ok else msg} [#{cid}]"
            self.elapsed = round(total, 2) if total is not None else None
            if self.state in ("done", "error"):
                self.state = "done" if ok else "error"
                self.error = "" if ok else msg
        log(f"명령 #{cid} {name} {'성공' if ok else '실패'}"
            + (f" · 발화종료→동작 {total:.2f}s" if total is not None else ""))
        self.stats.print_summary()
        return {"ok": True}

    def status(self):
        with self.lock:
            text = self.result_text
            if (self.state == "done" and text.startswith("⏳")
                    and time.monotonic() - self.last_bridge_seen > 3.0):
                text = "⚠ 로봇 서버 브리지 미연결 (명령 대기)"
            return {"state": self.state, "message": self.message, "text": text,
                    "elapsed": self.elapsed, "mic": self.mic.name,
                    "error": self.error, "model": MODEL_LABEL}

    def close(self):
        self.closed.set()
        self.stop_event.set()
        self.mic.close()


# =========================================================
# HTTP (localhost 전용)
# =========================================================
SERVICE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_data(self, obj, code=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/status":
            self.send_data(SERVICE.status())
        elif url.path == "/command":
            try:
                wait = float(parse_qs(url.query).get("wait", ["1.0"])[0])
            except ValueError:
                wait = 1.0
            self.send_data({"command": SERVICE.take_command(wait)})
        elif url.path == "/stats":
            self.send_data({"latency": SERVICE.stats.summary(),
                            "usage": SERVICE.limiter.summary(), "rss_mb": rss_mb()})
        else:
            self.send_data({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/start":
            with SERVICE.action_lock:
                self.send_data(SERVICE.start())
        elif path == "/stop":
            with SERVICE.action_lock:
                self.send_data(SERVICE.stop())
        elif path == "/ping":
            self.send_data(SERVICE.ping())
        elif path == "/result":
            try:
                n = min(int(self.headers.get("Content-Length", "0")), 4000)
                data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                data = {}
            self.send_data(SERVICE.report_result(data))
        else:
            self.send_data({"error": "not found"}, 404)


# =========================================================
# 메뉴형 단계별 테스트 (로봇 제어 없음)
# =========================================================
def wait_until_ready(svc, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = svc.status()["state"]
        if st != "loading":
            return st
        time.sleep(0.2)
    return "loading"


def record_once(svc):
    """녹음 1회 (무음 감지 또는 10초 자동 종료). 결과 명령 dict 또는 None."""
    st = svc.start()
    if st["state"] != "recording":
        print("  녹음 시작 실패:", st.get("error") or st.get("message"))
        return None
    print("  🎤 말하세요 (말이 끝나면 자동 종료 · 최대 10초)")
    while svc.status()["state"] in ("recording", "opening", "processing"):
        time.sleep(0.05)
    return svc.take_command(wait=0.3)


def menu():
    global ALLOW_FALLBACK_MIC
    ALLOW_FALLBACK_MIC = True
    svc = VoiceService(use_heartbeat=False)
    print("모델 로딩 중...")
    st = wait_until_ready(svc)
    if st == "error":
        print("오류:", svc.status()["error"])
    try:
        while True:
            print("\n===== HazardScout 음성 테스트 (로봇 미동작) =====")
            print("1) 입력 장치 목록")
            print("2) Vosk 단독 인식 테스트 (Claude 호출 없음)")
            print("3) Claude 분석 테스트 (키보드 문장 입력)")
            print("4) 음성 → Vosk → Claude → 검증 (MOCK)")
            print("5) 지연 통계 보기")
            print("0) 종료")
            sel = input("선택: ").strip()
            if sel == "1":
                for idx, name, ch, rate in svc.mic.list_devices():
                    print(f"  [{idx}] {name} · {ch}ch · {rate}Hz")
            elif sel == "2":
                svc.stt_only = True                       # Claude로 보내지 않음
                cmd = record_once(svc)
                svc.stt_only = False
                if cmd:
                    print("  🛑 긴급 정지 단어 감지 →", json.dumps(cmd, ensure_ascii=False))
                else:
                    print("  결과:", svc.status()["text"], "(문장은 출력하지 않습니다)")
            elif sel == "3":
                if svc.analyzer is None or svc.analyzer.error:
                    print("  ", svc.analyzer.error if svc.analyzer else "준비 안 됨")
                    continue
                line = input("  명령 문장: ")
                t0 = time.monotonic()
                obj, err = svc.analyzer.analyze(line)
                line = None
                cmd = svc.validator.validate(obj) if not err else None
                print(f"  Claude {time.monotonic() - t0:.2f}s →", err or json.dumps(cmd))
            elif sel == "4":
                cmd = record_once(svc)
                print("  결과 명령:", json.dumps(cmd, ensure_ascii=False) if cmd else
                      svc.status()["text"])
                if cmd:
                    svc.report_result({"id": cmd["id"], "ok": True, "command": cmd["command"],
                                       "action_start": time.monotonic(),
                                       "done": time.monotonic(), "message": "MOCK"})
            elif sel == "5":
                print(json.dumps(svc.stats.summary(), ensure_ascii=False, indent=2))
                print(json.dumps(svc.limiter.summary(), ensure_ascii=False))
            elif sel == "0":
                break
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        svc.close()
        print("종료")


# =========================================================
# main
# =========================================================
def main():
    global SERVICE
    if "--menu" in sys.argv:
        menu()
        return
    SERVICE = VoiceService()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log(f"localhost:{PORT} · {MODEL_LABEL} · 최대 {MAX_RECORD_SECONDS:.0f}초 · 음성 파일 저장 안 함")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        SERVICE.close()
        log("음성 서비스 종료")


if __name__ == "__main__":
    main()
