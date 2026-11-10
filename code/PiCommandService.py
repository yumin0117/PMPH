#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 요청 처리. 터미널 input() 없이 로그만 출력한다."""
import atexit
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import PiCommandCore as core


def load_settings():
    """API에 전달할 키는 PiCommandCore.py의 값만 읽는다."""
    key = core.CLAUDE_API_KEY.strip()
    port = int(core.COMMAND_PORT)
    if not 1 <= port <= 65535:
        raise ValueError('COMMAND_PORT 설정 확인')
    return key, port


class GuiRobot(core.Robot):
    """기존 수동 제어를 멈춘 뒤 공통 동작을 수행하고 PWM 위치 캐시를 맞춘다."""
    def __init__(self, namespace):
        super().__init__(check_server=False)
        self.ns = namespace

    def pause_manual(self):
        self.ns['safe_stop']()
        neck = self.ns.get('neck_controller')
        if neck is not None:
            neck.release_all()
        grip = self.ns.get('grip_controller')
        if grip is not None:
            grip.release()
        for name in ('arm_servo', 'hand_servo', 'gripper_servo', 'camera_servo'):
            controller = self.ns.get(name)
            if controller is not None:
                controller.stopWiggle()
        # 기존 singleServo 작업이 마지막 갱신을 마칠 때까지 대기.
        time.sleep(.12)

    def _sync(self):
        if self.pca is None:
            return
        by_channel = {0: self.steering, 4: self.gripper}
        by_channel.update({core.NECK_CHANNELS[a]: servo for a, servo in self.neck.items()})
        for channel, actuator in by_channel.items():
            angle = actuator.angle
            if angle is None:
                continue
            for name in ('steering', 'arm_servo', 'hand_servo', 'gripper_servo', 'camera_servo'):
                controller = self.ns.get(name)
                if controller is not None:
                    for field in ('nowPos', 'lastPos', 'bufferPos', 'goalPos'):
                        values = getattr(controller, field, None)
                        if values is not None:
                            values[channel] = angle
            neck = self.ns.get('neck_controller')
            if neck is not None:
                for axis, ch in core.NECK_CHANNELS.items():
                    if ch == channel:
                        neck.sync_pwm(axis, angle)

    def execute(self, item, cancel_event=None):
        self.pause_manual()
        if cancel_event is not None and cancel_event.is_set():
            raise core.CommandCancelled('취소된 명령')
        command = item['command']
        self.ns['current_motion'] = command if command in core.DRIVE_COMMANDS | core.TURN_COMMANDS else 'stop'
        try:
            self.ns['oled_set_direction'](self.ns['current_motion'])
            self.ns['apply_motion_lights'](self.ns['current_motion'])
            super().execute(item, cancel_event)
        finally:
            try:
                self._sync()
            finally:
                self.ns['safe_stop']()


class CommandService:
    def __init__(self, robot, stt=None, key='', analyzer=None):
        self.robot = robot
        self.stt = stt or core.STTClient()
        self.key = key
        self.analyzer = analyzer or core.analyze
        self.lock = threading.Lock()
        self.motion_lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.worker = None
        self.closed = False
        self.http_server = self.http_thread = None
        self.data = {'state': 'ready', 'busy': False, 'job_id': '', 'text': '', 'command': None,
                     'message': '명령 입력 준비 완료', 'error': '', 'recording_until': 0}

    def status(self):
        with self.lock:
            data = dict(self.data)
        data['remaining'] = max(0, data.pop('recording_until', 0) - time.monotonic())
        data['api_key_configured'] = bool(self.key)
        return data

    def _set(self, **values):
        with self.lock:
            self.data.update(values)

    def submit(self, mode, text=''):
        if mode not in ('text', 'voice'):
            raise ValueError('입력 방식 오류')
        if mode == 'voice':
            text = ''
        if mode == 'text' and (not isinstance(text, str) or not text.strip() or len(text) > 500):
            raise ValueError('텍스트는 1~500자로 입력하세요')
        with self.lock:
            if self.closed:
                raise RuntimeError('서비스 종료 중')
            if self.data['busy']:
                raise RuntimeError('명령 처리 중입니다. 완료 후 다시 입력하세요')
            if not self.key:
                raise RuntimeError('로봇의 PiCommandCore.py 상단 CLAUDE_API_KEY에 키를 입력하세요')
            cancel = threading.Event()
            self.cancel_event = cancel
            self.data = {'state': 'loading' if mode == 'voice' else 'analyzing', 'busy': True,
                         'job_id': uuid.uuid4().hex, 'text': text.strip(), 'command': None,
                         'message': '마이크/모델 준비 중' if mode == 'voice' else 'api 전달중',
                         'error': '', 'recording_until': 0}
            self.worker = threading.Thread(target=self._run, args=(mode, text.strip(), cancel),
                                           daemon=True, name='PiCommandWorker')
            self.worker.start()
        return self.status()

    def _check(self, cancel):
        if cancel.is_set() or self.closed:
            raise core.CommandCancelled('수동 조작/중지로 명령을 취소했습니다')

    def _run(self, mode, text, cancel):
        try:
            self._check(cancel)
            with self.motion_lock:
                self._check(cancel)
                self.robot.pause_manual()
            if mode == 'voice':
                self.stt.start()  # 살아 있는 공식 프로세스 재사용
                self._check(cancel)
                self._set(state='recording', message='지금 말하시오',
                          recording_until=time.monotonic() + core.COMMAND_WINDOW_SECONDS)
                text = self.stt.record()
                self._set(recording_until=0)
                if not text:
                    self._check(cancel)
                    raise ValueError('인식된 문장이 없습니다. 텍스트 입력으로도 사용할 수 있습니다.')
            self._check(cancel)
            print(f'[GUI {mode} 입력] {text}', flush=True)
            self._set(state='analyzing', text=text, message='api 전달중', recording_until=0)
            item = self.analyzer(text, self.key)
            self._check(cancel)
            print('[Claude 분석]', json.dumps(item, ensure_ascii=False), flush=True)
            self._set(command=item)
            if item['command'] != 'none':
                with self.motion_lock:
                    self._check(cancel)
                    self._set(state='executing', message=core.NAMES[item['command']] + ' 실행 중')
                    self.robot.execute(item, cancel)
            self._check(cancel)
            self._set(state='done', message=item['reason'])
        except core.CommandCancelled as exc:
            print('[AI 취소]', str(exc), flush=True)
            self._set(state='cancelled', message=str(exc))
        except Exception as exc:
            message = str(exc).replace(self.key, '[키 숨김]') if self.key else str(exc)
            print('[GUI 명령 오류]', message, flush=True)
            self._set(state='error', error=message, message=message)
        finally:
            self._set(busy=False, recording_until=0)

    def manual(self, callback, *args, **kwargs):
        # API 응답이 늦게 도착해도 수동 명령 뒤에 실행하지 않는다.
        with self.lock:
            self.cancel_event.set()
        with self.motion_lock:
            return callback(*args, **kwargs)

    def cancel(self):
        with self.lock:
            self.cancel_event.set()
        with self.motion_lock:
            self.robot.pause_manual()
            self.robot.stop()
        return self.status()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.cancel_event.set()
        if self.worker is not None:
            self.worker.join(timeout=core.API_TIMEOUT + 2)
        self.stt.close()
        with self.motion_lock:
            self.robot.stop()
            self.robot.close()
        if self.http_server is not None:
            self.http_server.shutdown()
            self.http_server.server_close()


def handle_command_http(handler, service):
    path = urlsplit(handler.path).path
    if not path.startswith('/api/ai/'):
        return False
    def send(data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    if service is None:
        send({'error': '명령 서비스 준비 중'}, 503)
        return True
    if handler.command == 'GET' and path == '/api/ai/status':
        send(service.status())
        return True
    if handler.command != 'POST' or path not in ('/api/ai/text', '/api/ai/voice', '/api/ai/cancel'):
        send({'error': '지원하지 않는 요청'}, 404)
        return True
    origin = handler.headers.get('Origin')
    if origin and origin != 'http://' + handler.headers.get('Host', ''):
        send({'error': '접속 출처 오류'}, 403)
        return True
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 <= length <= 4096:
            raise ValueError('요청 크기 오류')
        data = json.loads(handler.rfile.read(length).decode('utf-8') or '{}')
        if not isinstance(data, dict):
            raise ValueError('JSON 객체 필요')
        if path.endswith('/cancel'):
            send(service.cancel())
        else:
            send(service.submit('voice' if path.endswith('/voice') else 'text', data.get('text', '')), 202)
    except (ValueError, UnicodeDecodeError) as exc:
        send({'error': str(exc)}, 400)
    except RuntimeError as exc:
        send({'error': str(exc)}, 409)
    return True


def start_command_service(namespace):
    key, port = load_settings()
    service = CommandService(GuiRobot(namespace), key=key)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not handle_command_http(self, service):
                self.send_error(404)
        def do_POST(self):
            if not handle_command_http(self, service):
                self.send_error(404)
        def log_message(self, *_):
            pass
    service.http_server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    service.http_server.daemon_threads = True
    service.http_thread = threading.Thread(target=service.http_server.serve_forever, daemon=True,
                                          name='PiCommandHTTP')
    service.http_thread.start()
    atexit.register(service.close)
    print(f'[GUI 명령 서비스] 포트 {port} · 텍스트/로봇 마이크 → Claude → 동작', flush=True)
    if not key:
        print('[API 키 미설정] PiCommandCore.py 상단 CLAUDE_API_KEY에 키를 입력하고 서버를 다시 실행하세요.', flush=True)
    return service
