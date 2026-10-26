#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ReSpeaker Lite microphone + offline Korean STT. No audio file is written.

This server deliberately binds only to localhost. The PC and mobile GUIs
communicate via their existing Pi processes, not a public microphone port.
"""

import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

HOST = "127.0.0.1"
PORT = 5056
MODEL = os.environ.get("PICAR_VOICE_MODEL", "small")
MODEL_THREADS = 2
RATE = 16000
HINT = "A구역, B구역, C구역, 안전모, 근무자, 작업자, 전선, 공구, 위험물, 안전구역, 정리, 착용, 제거"


class VoiceEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.state = "loading"
        self.message = "한국어 인식 모델 준비 중"
        self.model = None
        self.error = ""
        self.text = ""
        self.elapsed = None
        self.mic_name = "미확인"
        self.mic_index = None
        self.rate = RATE
        self.chunks = []
        self.stream = None
        self.started_at = 0.0
        self.last_heartbeat = 0.0
        threading.Thread(target=self._load_model, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _load_model(self):
        try:
            model = WhisperModel(
                MODEL, device="cpu", compute_type="int8", cpu_threads=MODEL_THREADS,
                num_workers=1,
            )
            with self.lock:
                self.model = model
                if self.state == "loading":
                    self.state = "ready"
                    self.message = "음성 인식 준비 완료"
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.error = f"모델 준비 실패: {type(exc).__name__}: {exc}"
                self.message = self.error
            print("[Voice]", self.error, flush=True)

    def _find_microphone(self):
        devices = sd.query_devices()
        for idx, info in enumerate(devices):
            name = str(info["name"])
            if info["max_input_channels"] > 0 and (
                "respeaker" in name.lower() or "re speaker" in name.lower()
            ):
                return idx, info
        names = [str(d["name"]) for d in devices if d["max_input_channels"] > 0]
        raise RuntimeError(
            "ReSpeaker Lite 입력 장치가 없습니다. USB 펌웨어, USB 데이터 케이블, "
            "arecord -l을 확인하세요. 검색된 입력: " + ", ".join(names)
        )

    def _callback(self, indata, frames, timing, status):
        # PortAudio callback: copy only; never do ML inference in this callback.
        with self.lock:
            if self.state == "recording":
                self.chunks.append(indata[:, 0].copy())

    def start(self):
        with self.lock:
            if self.state not in ("ready", "done", "error") or self.model is None:
                return self.status()
            self.error = ""
            self.text = ""
            self.elapsed = None
            self.chunks = []
            self.state = "opening"
            self.message = "ReSpeaker Lite 연결 중"
        try:
            device, info = self._find_microphone()
            rates = [RATE]
            native = int(info["default_samplerate"])
            if native not in rates:
                rates.append(native)
            # ReSpeaker USB firmware may expose two processed/raw channels;
            # first channel is used for recognized speech.
            channels = 2 if int(info["max_input_channels"]) >= 2 else 1
            stream = None
            last_exc = None
            for rate in rates:
                try:
                    sd.check_input_settings(device=device, channels=channels,
                                            samplerate=rate, dtype="float32")
                    stream = sd.InputStream(device=device, channels=channels,
                                            samplerate=rate, dtype="float32",
                                            blocksize=1024, callback=self._callback)
                    break
                except Exception as exc:
                    last_exc = exc
            if stream is None:
                raise RuntimeError(f"마이크 샘플레이트 연결 실패: {last_exc}")
            with self.lock:
                self.stream = stream
                self.rate = rate
                self.mic_index = device
                self.mic_name = str(info["name"])
                self.started_at = time.monotonic()
                self.last_heartbeat = self.started_at
                self.state = "recording"
                self.message = "녹음 중 · 버튼을 놓으면 분석"
            stream.start()
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.error = f"마이크 열기 실패: {type(exc).__name__}: {exc}"
                self.message = self.error
            print("[Voice]", self.error, flush=True)
        return self.status()

    def ping(self):
        with self.lock:
            if self.state in ("opening", "recording"):
                self.last_heartbeat = time.monotonic()
        return self.status()

    def _watchdog(self):
        # If browser closes / Wi-Fi fails, don't leave the Pi microphone recording.
        while True:
            time.sleep(.5)
            with self.lock:
                expired = (self.state == "recording" and
                           time.monotonic() - self.last_heartbeat > 3.0)
            if expired:
                with self.action_lock:
                    with self.lock:
                        still_expired = (self.state == "recording" and
                            time.monotonic() - self.last_heartbeat > 3.0)
                    if still_expired:
                        print("[Voice] 연결 끊김 · 녹음 자동 종료", flush=True)
                        self.stop()

    def stop(self):
        with self.lock:
            if self.state not in ("recording", "opening"):
                return self.status()
            self.state = "processing"
            self.message = "한국어 인식 중"
            stream = self.stream
            self.stream = None
            chunks = self.chunks
            self.chunks = []
            rate = self.rate
        # Never block the HTTP control response on slow ASR.
        threading.Thread(target=self._recognize, args=(stream, chunks, rate), daemon=True).start()
        return self.status()

    def _recognize(self, stream, chunks, rate):
        try:
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
            if not chunks:
                raise ValueError("녹음된 소리가 없습니다")
            audio = np.concatenate(chunks).astype(np.float32, copy=False)
            if audio.size < int(rate * .20):
                raise ValueError("너무 짧은 녹음입니다")
            if rate != RATE:
                factor = math.gcd(int(rate), RATE)
                audio = resample_poly(audio, RATE // factor, int(rate) // factor)
                audio = np.asarray(audio, dtype=np.float32)
            peak = float(np.max(np.abs(audio)))
            if peak < .003:
                raise ValueError("입력 음량이 작습니다. 마이크 뮤트 및 입력 게인을 확인하세요")
            frame_len = 320
            count = len(audio) // frame_len
            if count > 0:
                rms = np.sqrt(np.mean(audio[:count * frame_len].reshape(count, frame_len) ** 2, axis=1))
                cutoff = max(.0025, float(np.percentile(rms, 95)) * .11)
                active = np.where(rms >= cutoff)[0]
                if len(active):
                    lo = max(0, int(active[0] * frame_len - RATE * .24))
                    hi = min(len(audio), int((active[-1] + 1) * frame_len + RATE * .33))
                    audio = audio[lo:hi]
            if peak < .20:
                audio = np.clip(audio * min(3.0, .25 / max(peak, .001)), -1, 1)
            audio = np.ascontiguousarray(audio, dtype=np.float32)
            t0 = time.monotonic()
            # ndarray avoids PyAV's metadata_errors file-decoding path.
            segments, _ = self.model.transcribe(
                audio, language="ko", task="transcribe", beam_size=2,
                best_of=1, temperature=0.0, condition_on_previous_text=False,
                initial_prompt=HINT, vad_filter=False, without_timestamps=True,
            )
            result = " ".join(s.text.strip() for s in segments if s.text.strip()).strip()
            elapsed = round(time.monotonic() - t0, 2)
            with self.lock:
                self.text = result or "음성을 인식하지 못했습니다"
                self.elapsed = elapsed
                self.state = "done"
                self.message = "음성 인식 완료"
            print(f"[Voice] {elapsed}s: {self.text}", flush=True)
        except Exception as exc:
            with self.lock:
                self.state = "error"
                self.error = f"인식 오류: {type(exc).__name__}: {exc}"
                self.message = self.error
            print("[Voice]", self.error, flush=True)
        finally:
            chunks.clear()

    def status(self):
        with self.lock:
            return {"state": self.state, "message": self.message, "text": self.text,
                    "elapsed": self.elapsed, "mic": self.mic_name,
                    "error": self.error, "model": MODEL}


ENGINE = None


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
        if self.path == "/status":
            self.send_data(ENGINE.status())
        else:
            self.send_data({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/start":
            with ENGINE.action_lock:
                self.send_data(ENGINE.start())
        elif self.path == "/stop":
            with ENGINE.action_lock:
                self.send_data(ENGINE.stop())
        elif self.path == "/ping":
            self.send_data(ENGINE.ping())
        else:
            self.send_data({"error": "not found"}, 404)


def main():
    global ENGINE
    ENGINE = VoiceEngine()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"[Voice] localhost:{PORT}, model={MODEL}, audio files=disabled", flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with ENGINE.lock:
            stream = ENGINE.stream
            ENGINE.stream = None
            ENGINE.state = "closed"
        if stream:
            stream.stop()
            stream.close()


if __name__ == "__main__":
    main()
