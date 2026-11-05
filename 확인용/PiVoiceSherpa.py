#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ReSpeaker Lite → 기존 한국어 Sherpa 모델 → PC/모바일 GUI.

GUI의 /status /start /stop /ping 계약 유지. 이번 버전은 STT만 수행한다.
모터 제어 및 Claude 호출은 하지 않는다. 오디오는 메모리로만 처리한다.
"""
import json
import os
import select
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 5056
MODEL_LABEL = "sherpa-onnx-streaming-zipformer-korean-2024-06-16"
MODEL_DIR = Path(os.environ.get("PICAR_SHERPA_MODEL", str(Path(__file__).resolve().parent / "models" / MODEL_LABEL)))
# 숫자 카드 번호가 바뀌어도 ReSpeaker Lite를 선택한다.
MIC_DEVICE = os.environ.get("PICAR_SHERPA_MIC", "plughw:CARD=Lite,DEV=0")
MIC_CHANNELS = int(os.environ.get("PICAR_SHERPA_CHANNELS", "2"))
SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 10.0
HEARTBEAT_TIMEOUT = 3.0
ENGINE = "sherpa"


class VoiceService:
    def __init__(self):
        self.lock = threading.RLock()
        self.state, self.message = "loading", "한국어 Sherpa 모델 준비 중"
        self.text, self.error, self.elapsed = "", "", None
        self.recognizer = None
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.last_ping = 0.0
        self.process = None
        self.worker = None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            import sherpa_onnx
            files = {
                "tokens": "tokens.txt",
                "encoder": "encoder-epoch-99-avg-1.int8.onnx",
                "decoder": "decoder-epoch-99-avg-1.onnx",
                "joiner": "joiner-epoch-99-avg-1.int8.onnx",
            }
            paths = {key: str(MODEL_DIR / name) for key, name in files.items()}
            for path in paths.values():
                if not Path(path).is_file():
                    raise FileNotFoundError(path)
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                **paths, num_threads=2, sample_rate=SAMPLE_RATE,
                feature_dim=80, decoding_method="greedy_search", provider="cpu",
            )
            with self.lock:
                self.state, self.message = "ready", "Sherpa 한국어 인식 준비 완료"
            print("[음성] Sherpa 한국어 모델 준비 완료 · ReSpeaker Lite 사용", flush=True)
        except Exception as exc:
            self._error("모델 준비 실패: " + str(exc))

    def _error(self, message):
        with self.lock:
            self.error = message
            self.message = message
            self.state = "error"
        print("[음성] " + message, flush=True)

    def status(self):
        with self.lock:
            return {"state": self.state, "message": self.message,
                    "text": self.text, "error": self.error, "elapsed": self.elapsed,
                    "mic": MIC_DEVICE, "model": MODEL_LABEL, "engine": ENGINE}

    def start(self):
        with self.lock:
            if self.closed.is_set() or self.recognizer is None:
                return self.status()
            if self.worker is not None and self.worker.is_alive():
                return self.status()
            self.stop_event.clear()
            self.last_ping = time.monotonic()
            self.state, self.message = "opening", "ReSpeaker Lite 연결 중"
            self.text, self.error, self.elapsed = "", "", None
            self.worker = threading.Thread(target=self._record, daemon=True, name="SherpaRecord")
            self.worker.start()
            return self.status()

    def stop(self):
        self.stop_event.set()
        return self.status()

    def ping(self):
        with self.lock:
            self.last_ping = time.monotonic()
        return self.status()

    @staticmethod
    def _terminate(proc):
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)

    def _record(self):
        proc = None
        started = time.monotonic()
        try:
            import numpy as np
            stream = self.recognizer.create_stream()
            proc = subprocess.Popen(
                ["arecord", "-q", "-D", MIC_DEVICE, "-t", "raw", "-f", "S16_LE",
                 "-r", str(SAMPLE_RATE), "-c", str(MIC_CHANNELS)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            with self.lock:
                self.process = proc
                self.state, self.message = "recording", "녹음 중 · 놓으면 한국어 인식"
            started = time.monotonic()
            received = 0
            remainder = b""
            frame_bytes = 2 * MIC_CHANNELS
            limit = int(MAX_RECORD_SECONDS * SAMPLE_RATE)
            last_audio = started
            while not self.stop_event.is_set() and not self.closed.is_set():
                now = time.monotonic()
                with self.lock:
                    heartbeat_expired = now - self.last_ping > HEARTBEAT_TIMEOUT
                if now - started >= MAX_RECORD_SECONDS or received >= limit or heartbeat_expired:
                    break
                if not select.select([proc.stdout], [], [], 0.05)[0]:
                    if now - last_audio > 3.0:
                        raise RuntimeError("ReSpeaker 입력 없음 · USB 연결과 마이크 점유 확인")
                    continue
                chunk = os.read(proc.stdout.fileno(), int(SAMPLE_RATE * .1) * frame_bytes)
                if not chunk:
                    proc.wait(timeout=1)
                    detail = proc.stderr.read(2000).decode("utf-8", errors="replace").strip()
                    raise RuntimeError(detail or "ReSpeaker 녹음 프로세스 종료")
                last_audio = time.monotonic()
                chunk = remainder + chunk
                usable = len(chunk) // frame_bytes * frame_bytes
                remainder = chunk[usable:]
                if not usable:
                    continue
                # Lite의 첫 채널 사용. 브라우저/PC 마이크를 열지 않는다.
                samples = np.frombuffer(chunk[:usable], dtype="<i2").reshape(-1, MIC_CHANNELS)[:, 0]
                samples = samples[:limit - received].astype(np.float32) / 32768.0
                received += samples.size
                stream.accept_waveform(SAMPLE_RATE, samples)
                while self.recognizer.is_ready(stream):
                    self.recognizer.decode_stream(stream)
            self._terminate(proc)
            if self.closed.is_set():
                return
            with self.lock:
                self.state, self.message = "processing", "Sherpa 한국어 인식 중"
            # 스트리밍 모델의 마지막 토큰까지 처리한다.
            stream.accept_waveform(SAMPLE_RATE, np.zeros(int(.8 * SAMPLE_RATE), dtype=np.float32))
            stream.input_finished()
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
            text = self.recognizer.get_result(stream).strip() if received else ""
            with self.lock:
                self.text = text
                self.elapsed = round(time.monotonic() - started, 2)
                self.state = "done"
                self.message = "한국어 인식 완료" if text else "인식된 말이 없습니다. 다시 말해주세요."
            print("[음성] 인식: " + (text or "(없음)"), flush=True)
        except Exception as exc:
            self._error("음성 입력 실패: " + str(exc))
        finally:
            self._terminate(proc)
            if proc is not None:
                proc.stdout.close()
                proc.stderr.close()
            with self.lock:
                self.process = None

    def close(self):
        self.closed.set()
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=3)
        self._terminate(self.process)


SERVICE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_data(self, data, code=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/status":
            self.send_data(SERVICE.status())
        else:
            self.send_data({"error": "not found"}, 404)

    def do_POST(self):
        action = self.path.split("?", 1)[0]
        if action in ("/start", "/stop", "/ping"):
            self.send_data(getattr(SERVICE, action[1:])())
        else:
            self.send_data({"error": "not found"}, 404)


def main():
    global SERVICE
    # 먼저 포트를 확보해 기존 Vosk/Whisper 서비스와의 충돌을 분명히 알린다.
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    SERVICE = VoiceService()

    def stop_signal(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)
    try:
        print(f"[음성] Sherpa GUI 서비스 {HOST}:{PORT}", flush=True)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        print("[음성] 종료", flush=True)
    finally:
        SERVICE.close()
        server.server_close()


if __name__ == "__main__":
    main()
