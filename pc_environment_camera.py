#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""컴퓨터 USB 환경카메라 → PiCar 웹 GUI용 MJPEG.

Windows PowerShell, 이 파일을 저장한 폴더에서 실행:
    py -m pip install opencv-python
    py pc_environment_camera.py
다른 카메라가 나오면:
    py pc_environment_camera.py --camera 1
카메라 앱은 먼저 닫으세요. PC/로봇/모바일은 같은 네트워크를 사용하세요.
GUI 환경카메라의 톱니바퀴에 http://컴퓨터IPv4:8081/video_feed 입력.
이 프로그램을 실행하는 동안 같은 네트워크에서 영상 주소에 접속할 수 있습니다.
종료: Ctrl+C. 로봇/음성/기존 GUI 파일을 수정하지 않습니다.
"""

import argparse
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Frames:
    def __init__(self):
        self.condition = threading.Condition()
        self.jpeg = None
        self.sequence = 0
        self.error = ""
        self.stopped = False

    def publish(self, jpeg):
        with self.condition:
            self.jpeg = jpeg
            self.sequence += 1
            self.condition.notify_all()

    def wait(self, previous, timeout=5):
        with self.condition:
            self.condition.wait_for(
                lambda: self.stopped or (self.jpeg is not None and self.sequence != previous), timeout)
            if self.stopped or self.sequence == previous or self.jpeg is None:
                return None
            return self.sequence, self.jpeg

    def stop(self, error=""):
        with self.condition:
            self.error = error
            self.stopped = True
            self.condition.notify_all()


def capture(cap, frames, stop_event, cv2, fps):
    failures = 0
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            ok, image = cap.read()
            if not ok or image is None:
                failures += 1
                if failures >= 20:
                    raise RuntimeError("카메라 영상을 읽지 못했습니다. USB 연결과 카메라 앱 점유를 확인하세요.")
                stop_event.wait(.1)
                continue
            failures = 0
            ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise RuntimeError("카메라 영상 JPEG 변환 실패")
            frames.publish(encoded.tobytes())
            stop_event.wait(max(0, 1 / fps - (time.monotonic() - started)))
    except Exception as exc:
        print("[카메라 오류]", exc, flush=True)
        frames.stop(str(exc))
    finally:
        cap.release()
        frames.stop(frames.error)


def make_handler(frames):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                body = ('<!doctype html><meta charset="utf-8"><title>환경카메라</title>'
                        '<body style="margin:0;background:#171a1f;color:white;font-family:sans-serif">'
                        '<h3 style="margin:12px">컴퓨터 USB 환경카메라</h3>'
                        '<img src="/video_feed" style="max-width:100%;height:auto">'
                        '<p style="margin:12px">PiCar 환경카메라 ⚙에 이 컴퓨터의 /video_feed 주소를 입력하세요.</p>'
                        '</body>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path not in ('/video_feed', '/snapshot.jpg'):
                self.send_error(404)
                return
            packet = frames.wait(-1)
            if packet is None:
                self.send_error(503, 'Camera unavailable')
                return
            self.connection.settimeout(10)
            self.send_response(200)
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            if path == '/snapshot.jpg':
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(packet[1])))
                self.end_headers()
                self.wfile.write(packet[1])
                return
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            try:
                while packet is not None:
                    sequence, jpeg = packet
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                     str(len(jpeg)).encode('ascii') + b'\r\n\r\n' + jpeg + b'\r\n')
                    self.wfile.flush()
                    packet = frames.wait(sequence)
            except (OSError, TimeoutError):
                pass

        def log_message(self, fmt, *args):
            # 영상 요청은 계속 이어지므로 오류만 표시한다.
            if args and str(args[1] if len(args) > 1 else '').startswith(('4', '5')):
                print('[HTTP]', fmt % args, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description='컴퓨터 USB 환경카메라 MJPEG 서버')
    parser.add_argument('--camera', type=int, default=0, help='카메라 번호: 기본 0, 다른 카메라면 1 또는 2')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--fps', type=int, default=10)
    parser.add_argument('--backend', choices=['auto', 'dshow', 'msmf'], default='auto')
    args = parser.parse_args()
    if args.camera < 0 or not 1 <= args.port <= 65535 or not 1 <= args.fps <= 30:
        parser.error('카메라 번호는 0 이상, 포트는 1~65535, FPS는 1~30이어야 합니다.')
    try:
        import cv2
    except ImportError:
        print('먼저 컴퓨터에서 실행하세요: py -m pip install opencv-python')
        return 1
    if args.backend != 'auto' and sys.platform != 'win32':
        parser.error('dshow/msmf는 Windows용입니다.')
    backend = (cv2.CAP_DSHOW if sys.platform == 'win32' else cv2.CAP_ANY) if args.backend == 'auto' else {
        'dshow': cv2.CAP_DSHOW, 'msmf': cv2.CAP_MSMF}[args.backend]
    cap = cv2.VideoCapture(args.camera, backend)
    if not cap.isOpened():
        cap.release()
        print('[연결 실패] 카메라 앱을 닫고 --camera 1 또는 --backend msmf로 다시 실행해보세요.')
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
    frames, stop_event = Frames(), threading.Event()
    worker = threading.Thread(target=capture, args=(cap, frames, stop_event, cv2, args.fps), daemon=True)
    server = None
    worker.start()
    try:
        if frames.wait(-1, timeout=15) is None:
            raise RuntimeError(frames.error or '카메라 첫 영상 수신 시간 초과')
        server = ThreadingHTTPServer(('0.0.0.0', args.port), make_handler(frames))
        server.daemon_threads = True
        print(f'[영상 준비] 카메라 번호 {args.camera} · Ctrl+C로 종료', flush=True)
        print(f'컴퓨터에서 먼저 확인: http://127.0.0.1:{args.port}/', flush=True)
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                                if not info[4][0].startswith(('127.', '169.254.'))})
        except socket.gaierror:
            addresses = []
        print('GUI 환경카메라 ⚙에 입력할 주소 후보 (현재 Wi-Fi/이더넷 IPv4 선택):', flush=True)
        for address in addresses:
            print(f'  http://{address}:{args.port}/video_feed', flush=True)
        if not addresses:
            print(f'  http://컴퓨터IPv4:{args.port}/video_feed (ipconfig로 IPv4 확인)', flush=True)
        print('이 창을 열어두세요. PC·로봇·모바일은 같은 네트워크에 연결하세요.', flush=True)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        print('\n[종료 요청]')
    except (OSError, RuntimeError) as exc:
        print('[실행 실패]', exc)
        return 1
    finally:
        stop_event.set()
        frames.stop()
        if server is not None:
            server.server_close()
        worker.join(timeout=3)
        print('[종료] 환경카메라 서버 종료')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
