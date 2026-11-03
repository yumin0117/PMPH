#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import cv2
import torch
from picamera2 import Picamera2
import Move as move
import RPIservo
from SmoothServo import SmoothNeck, SmoothGripper  # SMOOTH_AB_INTEGRATION_v1

neck_controller = None
grip_controller = None
import Switch as switch
from OLED_eyes import OLEDEyes
from YOLO_detector import YOLODetector

from PiVoiceLauncher import start_voice, voice_request

# =========================================================
# CPU 부하 제한

# =========================================================
try:
    torch.set_num_threads(1)
except Exception:
    pass
try:
    torch.set_num_interop_threads(1)
except Exception:
    pass
try:
    cv2.setNumThreads(1)
except Exception:
    pass

# =========================================================
# 서버 / 주행 설정

# =========================================================
HOST = "0.0.0.0"
WEB_PORT = 5000
DRIVE_SPEED = 60
TURN_SPEED = 50
TURN_ANGLE = 30
Dv = -1

# =========================================================
# 카메라 설정

# =========================================================
CAMERA_FIXED_ANGLE = 90
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
# 모바일 안정성을 위해 15 -> 12
CAMERA_FPS = 12
# 전송량과 JPEG 압축 부하 감소
JPEG_QUALITY = 60

# =========================================================
# YOLO 설정

# =========================================================
YOLO_MODEL_PATH = "/home/pi/Adeept_PiCar-Pro/Server/models/hazard_3class_v1.pt"
YOLO_CONFIDENCE = 0.35
# 기존 256 -> 224
YOLO_IMAGE_SIZE = 224
# 기존 5 -> 8
YOLO_DETECT_EVERY_N_FRAMES = 8

# =========================================================
# 집게 설정

# =========================================================
GRIPPER_SPEED = 5

# =========================================================
# 프로그램 상태

# =========================================================
server_running = True
light_always_on = False
current_motion = "stop"
oled_eyes = None
yolo_detector = None
async_yolo = None
latest_jpeg = None
latest_frame_id = 0
camera_connected = False
camera_last_error = ""
control_lock = threading.RLock()
gripper_lock = threading.Lock()
camera_condition = threading.Condition()
last_move_seq = -1
move_seq_lock = threading.Lock()

# =========================================================
# Servo

# =========================================================
steering = RPIservo.ServoCtrl()
camera_servo = RPIservo.ServoCtrl()
gripper_servo = RPIservo.ServoCtrl()

# =========================================================
# 비동기 YOLO

# =========================================================

class AsyncYOLO:
    def __init__(self, detector):
        self.detector = detector
        self.frame_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.event = threading.Event()
        self.stop_event = threading.Event()
        self.latest_frame = None
        self.latest_detections = []
        self.thread = None
    def start(self):
        if self.detector is None:
            return
        self.thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="YOLOWorker"
        )
        self.thread.start()
    def submit(self, frame):
        if self.detector is None:
            return
        # 오래된 프레임을 쌓지 않고 가장 최신 프레임만 사용
        with self.frame_lock:
            self.latest_frame = frame.copy()
        self.event.set()
    def draw(self, frame):
        if self.detector is None:
            return frame
        with self.result_lock:
            detections = list(self.latest_detections)
        try:
            return self.detector._draw_detections(frame, detections)
        except Exception as e:
            print("[YOLO] 표시 오류:", repr(e))
            return frame
    def _worker(self):
        print("[YOLO] 비동기 탐지 Thread 시작")
        while not self.stop_event.is_set():
            self.event.wait(timeout=0.2)
            if self.stop_event.is_set():
                break
            if not self.event.is_set():
                continue
            self.event.clear()
            with self.frame_lock:
                if self.latest_frame is None:
                    continue
                frame = self.latest_frame.copy()
            try:
                detections = self.detector._run_inference(frame)
                with self.result_lock:
                    self.latest_detections = detections
            except Exception as e:
                print("[YOLO] 탐지 오류:", repr(e))
                time.sleep(0.05)
        print("[YOLO] 비동기 탐지 Thread 종료")
    def stop(self):
        self.stop_event.set()
        self.event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)

# =========================================================
# OLED / Servo 초기화

# =========================================================

def oled_set_direction(direction):
    if oled_eyes is None:
        return
    try:
        oled_eyes.set_direction(direction)
    except Exception as e:
        print("[OLED] 방향 전달 오류:", e)

def initialize_servos():
    steering.moveServoInit([0])
    # 모바일은 시작할 때 카메라 목을 한 번만 90도로 고정

    # 카메라 목 고정 해제: 초기 위치로 점프하지 않음
    # 이전 버전의 setDelay(0.12) 제거
    # RPIservo 기본 delay를 그대로 사용
    gripper_servo.start()
    print(f"[카메라 목] Servo 1 = {CAMERA_FIXED_ANGLE}도 고정")
    print("[집게] 추가 delay 없이 기본 속도로 동작")

# =========================================================
# 집게

# =========================================================

def gripper_start(direction):
    if grip_controller is None:
        raise RuntimeError("집게 제어기 초기화 전입니다")
    grip_controller.hold(direction)

def gripper_stop():
    if grip_controller is not None:
        grip_controller.release()

# =========================================================
# 라이트

# =========================================================

def both_lights_on():
    switch.switch(1, 1)
    switch.switch(2, 1)

def both_lights_off():
    switch.switch(1, 0)
    switch.switch(2, 0)

def apply_motion_lights(motion=None):
    if motion is None:
        motion = current_motion
    if light_always_on:
        both_lights_on()
        return
    if motion in ("forward", "backward"):
        both_lights_on()
    elif motion in ("left", "backleft"):
        switch.switch(1, 1)
        switch.switch(2, 0)
    elif motion in ("right", "backright"):
        switch.switch(1, 0)
        switch.switch(2, 1)
    else:
        both_lights_off()

def light_on_mode():
    global light_always_on
    light_always_on = True
    both_lights_on()
    print("[라이트] 항상 ON")

def light_off_mode():
    global light_always_on
    light_always_on = False
    apply_motion_lights(current_motion)
    print("[라이트] 자동 모드")

# =========================================================
# 주행

# =========================================================

def safe_stop(force_lights_off=False):
    global current_motion
    with control_lock:
        try:
            move.motorStop()
        except Exception:
            pass
        try:
            steering.moveAngle(0, 0)
        except Exception:
            pass
        current_motion = "stop"
        oled_set_direction("stop")
        try:
            if force_lights_off:
                both_lights_off()
            else:
                apply_motion_lights("stop")
        except Exception:
            pass

def drive_command(motion, direction, steering_offset, speed):
    global current_motion
    with control_lock:
        current_motion = motion
        oled_set_direction(motion)
        steering.moveAngle(
            0,
            steering_offset
        )
        move.move(
            speed,
            direction,
            "mid"
        )
        apply_motion_lights(motion)

def robot_ctrl(command, neck_seq=None):
    if command.startswith("neck:"):
        if neck_controller is None:
            raise RuntimeError("목 제어기 초기화 전입니다")
        neck_controller.command(command, neck_seq)
        return
    if command == "forward":
        drive_command(
            "forward",
            1,
            0,
            DRIVE_SPEED
        )
    elif command == "backward":
        drive_command(
            "backward",
            -1,
            0,
            DRIVE_SPEED
        )
    elif command == "left":
        drive_command(
            "left",
            1,
            TURN_ANGLE * Dv,
            TURN_SPEED
        )
    elif command == "right":
        drive_command(
            "right",
            1,
            -TURN_ANGLE * Dv,
            TURN_SPEED
        )
    elif command == "backleft":
        drive_command(
            "backleft",
            -1,
            TURN_ANGLE * Dv,
            TURN_SPEED
        )
    elif command == "backright":
        drive_command(
            "backright",
            -1,
            -TURN_ANGLE * Dv,
            TURN_SPEED
        )
    elif command == "stop_move":
        safe_stop(
            force_lights_off=False
        )
    elif command == "light_on":
        light_on_mode()
    elif command == "light_off":
        light_off_mode()
    elif command == "grab":
        gripper_start(-1)
    elif command == "loose":
        gripper_start(1)
    elif command == "grip_stop":
        gripper_stop()
    elif command in (
        "lookleft",
        "lookright",
        "lookup",
        "lookdown",
        "LRstop",
        "UDstop"
    ):
        pass
    else:
        raise ValueError(
            f"알 수 없는 명령: {command}"
        )

# =========================================================
# 카메라

# =========================================================

def set_camera_state(connected, error=""):
    global camera_connected
    global camera_last_error
    global latest_jpeg
    camera_connected = connected
    camera_last_error = error
    if not connected:
        # 이전 카메라 영상이 계속 얼어붙어 보이지 않도록 비움
        with camera_condition:
            latest_jpeg = None
            camera_condition.notify_all()

def camera_worker():
    global latest_jpeg
    global latest_frame_id
    frame_interval = 1.0 / CAMERA_FPS
    # 한 번 카메라 시작에 실패해도 Thread가 끝나지 않고
    # 서버가 실행 중인 동안 계속 다시 연결을 시도한다.
    while server_running:
        camera = None
        frame_count = 0
        try:
            print("[카메라] 연결 시도")
            camera = Picamera2()
            config = camera.create_preview_configuration(
                main={
                    "format": "RGB888",
                    "size": (
                        CAMERA_WIDTH,
                        CAMERA_HEIGHT
                    ),
                }
            )
            camera.configure(config)
            camera.start()
            time.sleep(1.0)
            set_camera_state(True)
            print(
                f"[카메라] 실시간 영상 시작 "
                f"{CAMERA_WIDTH}x{CAMERA_HEIGHT} "
                f"{CAMERA_FPS}FPS"
            )
            while server_running:
                loop_start = time.monotonic()
                frame = camera.capture_array()
                if frame is None:
                    raise RuntimeError(
                        "카메라 프레임을 받지 못했습니다."
                    )
                frame_bgr = cv2.cvtColor(
                    frame,
                    cv2.COLOR_RGB2BGR
                )
                frame_count += 1
                if (
                    async_yolo is not None
                    and
                    (
                        frame_count == 1
                        or
                        frame_count
                        %
                        YOLO_DETECT_EVERY_N_FRAMES
                        == 0
                    )
                ):
                    async_yolo.submit(
                        frame_bgr
                    )
                if async_yolo is not None:
                    frame_bgr = async_yolo.draw(
                        frame_bgr
                    )
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame_bgr,
                    [
                        int(
                            cv2.IMWRITE_JPEG_QUALITY
                        ),
                        JPEG_QUALITY
                    ]
                )
                if ok:
                    with camera_condition:
                        latest_jpeg = encoded.tobytes()
                        latest_frame_id += 1
                        camera_condition.notify_all()
                elapsed = (
                    time.monotonic()
                    -
                    loop_start
                )
                remaining = (
                    frame_interval
                    -
                    elapsed
                )
                if remaining > 0:
                    time.sleep(
                        remaining
                    )
        except Exception as e:
            error_text = repr(e)
            set_camera_state(
                False,
                error_text
            )
            print(
                "[카메라 오류]",
                error_text
            )
        finally:
            if camera is not None:
                try:
                    camera.stop()
                except Exception:
                    pass
                try:
                    camera.close()
                except Exception:
                    pass
        if server_running:
            print(
                "[카메라] 1초 후 재연결 시도"
            )
            time.sleep(1.0)
    set_camera_state(False)
    print(
        "[카메라] 종료"
    )

# =========================================================
# 모바일 GUI

# =========================================================
MOBILE_HTML = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta
name="viewport"
content="
width=device-width,
initial-scale=1,
maximum-scale=1,
user-scalable=no
"
>
<title>PiCar-Pro Mobile</title>
<style>
:root {
    --bg: #f4f6f8;
    --white: #ffffff;
    --text: #191f28;
    --sub: #8b95a1;
    --border: #e5e8eb;
    --blue: #3182f6;
    --blue-light: #eaf2ff;
    --green: #20a06b;
    --green-light: #e8f7f0;
    --camera: #171a1f;
    --joy-base: #505050;
    --joy-line: #c6c6c6;
    --joy-knob: #9b9b9b;
}
* {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
}
html,
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        "Noto Sans KR",
        sans-serif;
}
body {
    padding: 14px;
    padding-top:
        max(
            14px,
            env(safe-area-inset-top)
        );
    padding-bottom:
        max(
            20px,
            env(safe-area-inset-bottom)
        );
}
.app {
    width: 100%;
    max-width: 520px;
    margin: 0 auto;
}
.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
}
.title {
    font-size: 23px;
    font-weight: 800;
}
.subtitle {
    margin-top: 3px;
    color: var(--sub);
    font-size: 12px;
}
.server {
    padding: 8px 11px;
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: 12px;
    font-size: 12px;
}
.dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 5px;
    background: var(--green);
    border-radius: 50%;
}
.card {
    margin-bottom: 12px;
    padding: 14px;
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: 18px;
}
.card-title {
    margin-bottom: 10px;
    font-size: 15px;
    font-weight: 800;
}
.card-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 10px;
}
.card-title-row .card-title {
    margin-bottom: 0;
}
.camera-box {
    position: relative;
    width: 100%;
    aspect-ratio: 4 / 3;
    overflow: hidden;
    background: var(--camera);
    border-radius: 13px;
}
.camera {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    background: var(--camera);
}
.camera-state {
    position: absolute;
    left: 10px;
    bottom: 10px;
    padding: 5px 8px;
    color: white;
    background: rgba(0, 0, 0, 0.55);
    border-radius: 8px;
    font-size: 11px;
}
.motion {
    min-height: 24px;
    margin-bottom: 8px;
    text-align: center;
    color: var(--sub);
    font-weight: 700;
}
.row2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 9px;
}
.btn {
    min-height: 68px;
    padding: 8px;
    border: 1px solid var(--border);
    border-radius: 15px;
    background: var(--white);
    color: var(--text);
    font-size: 15px;
    font-weight: 800;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
}
.btn.active {
    background: var(--blue);
    color: white;
    border-color: var(--blue);
}
.light-on {
    background: var(--green-light);
    color: var(--green);
}
.light-off {
    background: var(--blue-light);
    color: var(--blue);
}
.light-selected {
    background: var(--green) !important;
    color: white !important;
}
.grab {
    background: var(--green-light);
    color: var(--green);
}
.loose {
    background: var(--blue-light);
    color: var(--blue);
}
.info {
    margin-top: 9px;
    color: var(--sub);
    font-size: 11px;
    line-height: 1.5;
}
.footer {
    padding: 2px 4px 10px;
    text-align: center;
    color: var(--sub);
    font-size: 10px;
}
.fullscreen-button {
    width: 42px;
    height: 38px;
    border: 1px solid var(--border);
    border-radius: 11px;
    background: var(--white);
    color: var(--text);
    font-size: 20px;
    font-weight: 800;
}
/* ======================================================
   원형 조이스틱
====================================================== */.joystick-wrap {
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 6px 0 2px;
}
.joystick {
    position: relative;
    width: min(68vw, 290px);
    height: min(68vw, 290px);
    border-radius: 50%;
    background: var(--joy-base);
    border: 6px solid var(--joy-line);
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
}
.joystick::before,
.joystick::after {
    content: "";
    position: absolute;
    left: 50%;
    top: 50%;
    transform:
        translate(
            -50%,
            -50%
        );
    background:
        rgba(
            235,
            235,
            235,
            0.62
        );
    pointer-events: none;
}
.joystick::before {
    width: 2px;
    height: 100%;
}
.joystick::after {
    width: 100%;
    height: 2px;
}
.joystick-knob {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 34%;
    height: 34%;
    transform:
        translate(
            -50%,
            -50%
        );
    border-radius: 50%;
    background: var(--joy-knob);
    box-shadow:
        0 4px 14px
        rgba(
            0,
            0,
            0,
            0.24
        );
    pointer-events: none;
}
.joystick-hint {
    margin-top: 10px;
    text-align: center;
    color: var(--sub);
    font-size: 11px;
}
/* ======================================================
   전체화면
====================================================== */.app.camera-fullscreen {
    position: fixed;
    inset: 0;
    z-index: 9999;
    width: 100vw;
    max-width: none;
    height: 100vh;
    margin: 0;
    padding: 12px;
    overflow: hidden;
    background: #0e1116;
    display: grid;
    grid-template-columns:
        minmax(0, 1fr)
        330px;
    grid-template-rows:
        auto auto 1fr;
    gap: 12px;
}
.app.camera-fullscreen .header,
.app.camera-fullscreen .footer {
    display: none;
}
.app.camera-fullscreen .card {
    margin: 0;
}
.app.camera-fullscreen .camera-card {
    grid-column: 1;
    grid-row: 1 / 4;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: #0e1116;
    border-color: #2b313a;
}
.app.camera-fullscreen .camera-card .card-title {
    color: white;
}
.app.camera-fullscreen .camera-box {
    flex: 1;
    min-height: 0;
    aspect-ratio: auto;
}
.app.camera-fullscreen .camera {
    object-fit: contain;
}
.app.camera-fullscreen .move-card {
    grid-column: 2;
    grid-row: 1;
}
.app.camera-fullscreen .light-card {
    grid-column: 2;
    grid-row: 2;
}
.app.camera-fullscreen .gripper-card {
    grid-column: 2;
    grid-row: 3;
}
.app.camera-fullscreen .joystick {
    width: min(28vw, 250px);
    height: min(28vw, 250px);
}
@media
(orientation: portrait),
(max-width: 700px) {
    .app.camera-fullscreen {
        overflow-y: auto;
        grid-template-columns: 1fr;
        grid-template-rows:
            55vh
            auto
            auto
            auto;
    }

    .app.camera-fullscreen .camera-card {
        grid-column: 1;
        grid-row: 1;
    }

    .app.camera-fullscreen .move-card {
        grid-column: 1;
        grid-row: 2;
    }

    .app.camera-fullscreen .light-card {
        grid-column: 1;
        grid-row: 3;
    }

    .app.camera-fullscreen .gripper-card {
        grid-column: 1;
        grid-row: 4;
    }

    .app.camera-fullscreen .joystick {
        width: min(64vw, 260px);
        height: min(64vw, 260px);
    }
}

/* VOICE_REPEAKER_1: Voice GUI */
.voice-card .voice-button {width:100%;border:0;border-radius:14px;padding:19px 12px;background:#3182F6;color:white;font-weight:750;font-size:17px;touch-action:none;user-select:none;-webkit-user-select:none;}
.voice-card .voice-button.recording {background:#D9534F;}
.voice-card .voice-text {margin-top:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.7;}
.voice-card .voice-hint {font-size:12px;opacity:.72;margin-top:9px;}


/* SMOOTH_AB_INTEGRATION_v1 : camera neck control */
.neck-control-card .neck-title {font-size:18px;font-weight:700;margin:0 0 8px;}
.neck-control-card .neck-note {font-size:12px;opacity:.75;line-height:1.5;margin-bottom:12px;}
.neck-control-card .neck-joystick {position:relative;width:min(62vw,240px);height:min(62vw,240px);border:4px solid #bbc1c9;border-radius:50%;background:#51565e;margin:16px auto;touch-action:none;}
.neck-control-card .neck-joystick::before,.neck-control-card .neck-joystick::after {content:"";position:absolute;left:50%;top:50%;background:#ffffff70;transform:translate(-50%,-50%);pointer-events:none;}
.neck-control-card .neck-joystick::before{width:2px;height:100%;}
.neck-control-card .neck-joystick::after{width:100%;height:2px;}
.neck-control-card .neck-knob{position:absolute;left:50%;top:50%;width:34%;height:34%;border-radius:50%;background:#b7bec7;transform:translate(-50%,-50%);pointer-events:none;box-shadow:0 3px 10px #0004;}
.neck-control-card .neck-b-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;}
.neck-control-card .neck-b-btn{border:1px solid #ccd1d8;border-radius:12px;background:#f4f6f8;color:#243142;font-weight:700;padding:17px 12px;touch-action:none;font-size:16px;}
.neck-control-card .neck-b-btn.active{background:#3182f6;color:#fff;}



/* 태블릿 배치 전용 추가 CSS: 원본 스타일과 Python/JS 제어 로직은 유지 */
.tablet-cameras,.tablet-joysticks,.tablet-tools{display:block;}
.environment-card .environment-config{font-size:19px;}
.environment-placeholder{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#aeb5bf;font-size:14px;pointer-events:none;}
.environment-note{margin-top:8px;color:#8b95a1;font-size:11px;}
.tablet-top-flash{background:var(--green-light);color:var(--green);border:1px solid var(--border);border-radius:12px;font-size:12px;font-weight:800;padding:9px 12px;white-space:nowrap;margin-left:auto;}
.tablet-top-flash.light-selected{background:var(--green)!important;color:#fff!important;}
.neck-motion{font-size:13px;}
/* 주행과 목 A 설명을 각각 원형 조이스틱 바로 아래 똑같은 높이에 배치 */
.tablet-joysticks .joystick-hint,.tablet-joysticks .neck-hint{color:var(--sub);text-align:center;line-height:1.35;font-size:11px;min-height:16px;margin-top:5px;}
/* 조이스틱 배경과 손잡이는 전체화면에서도 불투명 */
.app.camera-fullscreen .joystick,.app.camera-fullscreen .neck-joystick{opacity:1!important;background:#505050!important;background-color:#505050!important;filter:none!important;backdrop-filter:none!important;mix-blend-mode:normal!important;isolation:isolate;}
.app.camera-fullscreen .joystick-knob{background:#9b9b9b!important;opacity:1!important;filter:none!important;}
.app.camera-fullscreen .neck-knob{background:#b7bec7!important;opacity:1!important;filter:none!important;}
.app.camera-fullscreen .tablet-joysticks>.card{background:#fff!important;opacity:1!important;border:1px solid var(--border)!important;}
@media (max-width:739px),(orientation:portrait){
  .tablet-cameras>.card,.tablet-joysticks>.card,.tablet-tools>.card{margin-bottom:12px;}
}
@media (min-width:740px) and (orientation:landscape){
  body{padding:8px 12px 9px;}
  .app{width:100%;max-width:1600px;margin:0 auto;}
  .header{margin-bottom:7px;}
  .tablet-cameras,.tablet-joysticks{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:9px;}
  .tablet-tools{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;}
  .tablet-cameras>.card,.tablet-joysticks>.card,.tablet-tools>.card{min-width:0;margin:0;}
  .tablet-cameras .card{padding:12px 14px;}
  /* 카메라를 메인으로: 기존 160px -> 일반 태블릿에서 약 275~320px */
  .tablet-cameras .camera-box{aspect-ratio:auto;height:clamp(238px,37.5vh,420px);}
  .tablet-cameras .camera{object-fit:contain;}
  .tablet-cameras .card-title-row{margin-bottom:7px;}
  .tablet-joysticks>.card{padding:9px 14px 8px;display:flex;flex-direction:column;align-items:stretch;justify-content:flex-start;}
  .tablet-joysticks .card-title,.tablet-joysticks .neck-title{font-size:15px!important;margin-bottom:3px!important;font-weight:800;}
  .tablet-joysticks .motion{font-size:12px;min-height:18px;margin:0 0 2px!important;}
  .tablet-joysticks .joystick-wrap{padding:0;}
  .tablet-joysticks .joystick,.tablet-joysticks .neck-joystick{
    width:clamp(104px,17vh,170px);height:clamp(104px,17vh,170px);margin:2px auto!important;
  }
  .tablet-joysticks .joystick-hint{font-size:11px;min-height:15px;margin-top:4px;}
  .tablet-tools>.card{padding:10px 12px;}
  .tablet-tools .card-title,.tablet-tools .neck-title{font-size:14px!important;margin-bottom:6px!important;}
  .tablet-tools .btn{min-height:45px;font-size:13px;padding:5px;}
  .tablet-tools .neck-b-row{margin-top:5px;gap:6px;}
  .tablet-tools .neck-b-btn{font-size:13px;padding:10px 6px;}
  .tablet-tools .neck-note{font-size:10px;line-height:1.3;margin-bottom:4px;}
  .tablet-tools .voice-button{padding:10px 6px;font-size:13px;}
  .tablet-tools .voice-hint{display:none;}
  .tablet-tools .voice-text{font-size:11px;line-height:1.35;margin-top:6px;max-height:2.8em;overflow:auto;}
  .tablet-tools .info{font-size:10px;margin-top:7px;}
  .footer{display:none;}
  /* 카메라 전체화면: 기존 구조를 유지하되 2대 영상을 가장 크게 표시.
     조이스틱/손잡이는 화면 아래쪽에서도 100% 불투명한 원본 회색을 사용. */
  .app.camera-fullscreen{
    display:flex!important;flex-direction:column!important;position:fixed;inset:0;z-index:9999;
    height:100dvh;width:100vw;max-width:none;margin:0;padding:10px;overflow:auto;
    background:#0e1116;gap:7px;grid-template-columns:none;grid-template-rows:none;
  }
  .app.camera-fullscreen .header,.app.camera-fullscreen .footer{display:none!important;}
  .app.camera-fullscreen .tablet-cameras{
    display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:0;
    flex:1 1 auto;min-height:0;
  }
  .app.camera-fullscreen .tablet-cameras>.camera-card{
    grid-column:auto!important;grid-row:auto!important;min-height:0;display:flex;flex-direction:column;
    background:#0e1116!important;border:1px solid #2b313a!important;
  }
  .app.camera-fullscreen .tablet-cameras .card-title{color:#fff;}
  .app.camera-fullscreen .tablet-cameras .camera-box{flex:1 1 auto;min-height:0;height:auto!important;aspect-ratio:auto;}
  .app.camera-fullscreen .tablet-cameras .camera{object-fit:contain;}
  .app.camera-fullscreen .tablet-joysticks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:0;flex:0 0 auto;}
  .app.camera-fullscreen .tablet-joysticks>.card{grid-column:auto!important;grid-row:auto!important;min-height:0;}
  .app.camera-fullscreen .tablet-joysticks .joystick,.app.camera-fullscreen .tablet-joysticks .neck-joystick{
    width:clamp(105px,17vh,175px)!important;height:clamp(105px,17vh,175px)!important;
    opacity:1!important;background:#505050!important;
  }
  .app.camera-fullscreen .tablet-tools{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:0;flex:0 0 auto;}
  .app.camera-fullscreen .tablet-tools>.card{grid-column:auto!important;grid-row:auto!important;background:#fff!important;}
}
@media (min-width:740px) and (orientation:landscape) and (max-height:790px){
  .tablet-cameras .camera-box{height:clamp(230px,36vh,295px);}
  .tablet-joysticks .joystick,.tablet-joysticks .neck-joystick{width:clamp(103px,16vh,126px);height:clamp(103px,16vh,126px);}
  .tablet-tools>.card{padding:9px 11px;}
  .tablet-tools .btn{min-height:43px;}
  .tablet-tools .neck-b-btn{padding:9px 5px;}
}
@media (min-width:740px) and (orientation:landscape) and (max-height:650px){
  .tablet-cameras .camera-box{height:32vh;}
  .tablet-joysticks .joystick,.tablet-joysticks .neck-joystick{width:100px;height:100px;}
  .app.camera-fullscreen .tablet-tools{display:none!important;}
}


/* ===========================================================
   카메라별 전체화면 전용 UI
   - 평소 태블릿 화면은 기존 스타일/배치 그대로 유지
   - 카메라가 전체 배경을 차지하고, 조이스틱은 그 위에 불투명하게 표시
   - 기존 #joystick / #neckJoystick 노드를 잠시 이동해 기존 이벤트 유지
   =========================================================== */
#cameraFullscreenLayer[hidden] { display: none !important; }
#cameraFullscreenLayer {
  position: fixed;
  inset: 0;
  z-index: 100000;
  width: 100vw;
  height: 100vh;
  height: 100dvh;
  margin: 0;
  padding: 0;
  overflow: hidden;
  background: #090d12;
  color: #fff;
  display: block;
  isolation: isolate;
}
#cameraFullscreenLayer:fullscreen { width: 100vw; height: 100vh; }
#cameraFullscreenLayer .fs-video-backdrop,
#cameraFullscreenLayer .fs-main-image,
#cameraFullscreenLayer .fs-empty-image {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
#cameraFullscreenLayer .fs-video-backdrop { overflow: hidden; background: #090d12; z-index: 0; }
#cameraFullscreenLayer .fs-main-image {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #090d12;
}
#cameraFullscreenLayer .fs-empty-image {
  display: none;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: #b7c2cd;
  font-size: 15px;
  padding: 20px;
  background: #111821;
}
#cameraFullscreenLayer .fs-topbar {
  position: absolute;
  top: 0; left: 0; right: 0;
  z-index: 3;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: max(12px,env(safe-area-inset-top)) 16px 18px;
  background: linear-gradient(to bottom,rgba(0,0,0,.80),rgba(0,0,0,0));
  pointer-events: none;
}
#cameraFullscreenLayer .fs-camera-title {
  font-size: 17px;
  font-weight: 800;
  text-shadow: 0 1px 5px #000;
}
#cameraFullscreenLayer .fs-top-actions { display: flex; gap: 8px; align-items: center; pointer-events: auto; }
#cameraFullscreenLayer .fs-action {
  min-height: 40px;
  border-radius: 10px;
  border: 1px solid rgba(255,255,255,.5);
  background: #222b36;
  color: #fff;
  font-size: 13px;
  font-weight: 800;
  padding: 8px 12px;
  cursor: pointer;
  touch-action: manipulation;
}
#cameraFullscreenLayer .fs-action.stop { background: #b5333b; border-color: #f6b6b9; }
#cameraFullscreenLayer .fs-action.exit { background: #1f2935; }
#cameraFullscreenLayer .fs-bottom-controls {
  position: absolute;
  z-index: 4;
  left: 0; right: 0; bottom: 0;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  padding: 16px clamp(18px,4vw,60px) max(22px,env(safe-area-inset-bottom));
  pointer-events: none;
}
#cameraFullscreenLayer .fs-joy-unit {
  width: clamp(150px,26vw,240px);
  flex: 0 1 clamp(150px,26vw,240px);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  pointer-events: auto;
}
#cameraFullscreenLayer .fs-joy-label,
#cameraFullscreenLayer .fs-joy-status {
  color: #fff;
  padding: 4px 9px;
  border-radius: 8px;
  background: #1c2734;
  font-weight: 800;
  font-size: 12px;
  text-align: center;
  max-width: 100%;
  text-shadow: 0 1px 2px #000;
}
#cameraFullscreenLayer .fs-joy-status { font-size: 11px; font-weight: 600; }
#cameraFullscreenLayer .fs-joystick-holder {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
/* 기존 조이스틱 원형·색상 유지. 전체화면에서만 크기 지정, 완전 불투명. */
#cameraFullscreenLayer .fs-joystick-holder .joystick,
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick {
  width: clamp(145px,24vw,215px) !important;
  height: clamp(145px,24vw,215px) !important;
  max-width: min(31vh,215px);
  max-height: min(31vh,215px);
  flex: none;
  border-radius: 50%;
  background: #505050 !important;
  background-color: #505050 !important;
  border: 5px solid #c6c6c6 !important;
  opacity: 1 !important;
  filter: none !important;
  backdrop-filter: none !important;
  box-shadow: 0 5px 19px rgba(0,0,0,.58);
  margin: 0 !important;
  touch-action: none;
}
#cameraFullscreenLayer .fs-joystick-holder .joystick-knob,
#cameraFullscreenLayer .fs-joystick-holder .neck-knob {
  background: #9b9b9b !important;
  background-color: #9b9b9b !important;
  opacity: 1 !important;
  filter: none !important;
  backdrop-filter: none !important;
}
/* 기존 목 A 원은 원래 .neck-control-card 안에만 스타일이 정의되어 있으므로,
   전체화면으로 DOM 이동한 경우에도 십자선/손잡이 모양을 똑같이 복원한다. */
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick { position: relative !important; }
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick::before,
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick::after {
  content: "";
  position: absolute;
  left: 50%; top: 50%;
  transform: translate(-50%,-50%);
  background: rgba(255,255,255,.62);
  pointer-events: none;
}
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick::before { width: 2px; height: 100%; }
#cameraFullscreenLayer .fs-joystick-holder .neck-joystick::after { width: 100%; height: 2px; }
#cameraFullscreenLayer .fs-joystick-holder .neck-knob {
  position: absolute !important;
  left: 50%; top: 50%;
  width: 34% !important; height: 34% !important;
  border-radius: 50%;
  transform: translate(-50%,-50%);
  box-shadow: 0 4px 14px rgba(0,0,0,.3);
  pointer-events: none;
}
@media (orientation: portrait) {
  #cameraFullscreenLayer .fs-bottom-controls { padding: 12px 9px max(18px,env(safe-area-inset-bottom)); gap: 4px; }
  #cameraFullscreenLayer .fs-joy-unit { flex-basis: 50%; width: 50%; }
  #cameraFullscreenLayer .fs-joystick-holder .joystick,
  #cameraFullscreenLayer .fs-joystick-holder .neck-joystick {
    width: min(37vw,180px) !important;
    height: min(37vw,180px) !important;
  }
  #cameraFullscreenLayer .fs-camera-title { font-size: 13px; }
  #cameraFullscreenLayer .fs-action { padding: 6px 8px; min-height: 35px; font-size: 11px; }
}
@media (max-height: 550px) and (orientation: landscape) {
  #cameraFullscreenLayer .fs-joystick-holder .joystick,
  #cameraFullscreenLayer .fs-joystick-holder .neck-joystick {
    width: min(26vh,150px) !important;
    height: min(26vh,150px) !important;
  }
  #cameraFullscreenLayer .fs-bottom-controls { padding-bottom: 7px; }
  #cameraFullscreenLayer .fs-joy-unit { gap: 3px; }
}

</style>
</head>
<body>
<div class="app">
<!-- 기존 모바일 제목과 연결 상태 유지 -->
<div class="header">
<div>
<div class="title">
PiCar-Pro
</div>
<div class="subtitle">
Mobile Control
</div>
</div>
<button type="button" id="tabletFlash" class="tablet-top-flash" title="라이트 ON/OFF">후레시 ON</button>
<div class="server">
<span
id="serverDot"
class="dot"
></span>
<span id="serverText">
연결됨
</span>
</div>
</div>
<!-- 상단 좌/우 카메라 -->
<div class="tablet-cameras">
<div class="card camera-card">
<div class="card-title-row">
<div class="card-title">
로봇 카메라
</div>
<button
id="fullscreenButton"
class="fullscreen-button"
type="button"
>
⛶
</button>
</div>
<div class="camera-box">
<img
id="robotCamera"
class="camera"
src="/video_feed"
alt="Robot Camera"
>
<div
id="cameraState"
class="camera-state"
>
연결 중
</div>
</div>
</div>
<div class="card camera-card environment-card">
<div class="card-title-row">
<div class="card-title">환경 카메라</div>
<div style="display:flex;gap:7px;align-items:center;">
<button id="environmentFullscreenButton" class="fullscreen-button" type="button" title="환경 카메라 전체화면" aria-label="환경 카메라 전체화면">⛶</button>
<button id="environmentConfig" class="fullscreen-button environment-config" type="button" title="환경 카메라 주소 설정">⚙</button>
</div>
</div>
<div class="camera-box">
<img id="environmentCamera" class="camera" alt="환경 카메라" style="display:none" />
<div id="environmentPlaceholder" class="environment-placeholder">환경 카메라 연결 대기<div class="environment-note">영상 주소 설정 후 표시됩니다</div></div>
<div id="environmentState" class="camera-state">미연결</div>
</div>
</div>
</div>
<!-- 중단 좌 주행 / 우 목 A: 기존 원형 조이스틱과 JS ID 유지 -->
<div class="tablet-joysticks">
<div class="card move-card">
<div class="card-title">
이동 제어
</div>
<div
id="motion"
class="motion"
>
정지
</div>
<div class="joystick-wrap">
<div
id="joystick"
class="joystick"
>
<div
id="joystickKnob"
class="joystick-knob"
></div>
</div>
</div>
<div class="joystick-hint">
작은 원을 원하는 방향으로 움직이세요.
손을 떼면 자동 정지합니다.
</div>
</div>
<div class="card neck-control-card">
    <div class="neck-title">A · 카메라 목</div>
    <div id="neckStatus" class="motion neck-motion">목 A 정지</div>
    <div class="neck-joystick" id="neckJoystick">
        <div class="neck-knob" id="neckKnob"></div>
    </div>
    <div class="joystick-hint neck-hint">작은 원을 원하는 방향으로 움직이세요. 손을 떼면 현재 위치에 정지합니다.</div>
</div>
</div>
<!-- 라이트와 목 B 카드 위치 교환: 라이트 -> 집게 -> 목 B -> 마이크 -->
<div class="tablet-tools">
<div class="card light-card">
<div class="card-title">
라이트
</div>
<div class="row2">
<button
id="lightOn"
class="btn light-on"
>
LIGHT ON
</button>
<button
id="lightOff"
class="btn light-off"
>
LIGHT OFF
<br>
<small>
자동 모드
</small>
</button>
</div>
<div
id="lightText"
class="info"
>
현재: 자동 모드
</div>
</div>
<div class="card gripper-card">
<div class="card-title">
집게
</div>
<div class="row2">
<button
class="btn grip grab"
data-command="grab"
>
잡기
</button>
<button
class="btn grip loose"
data-command="loose"
>
놓기
</button>
</div>
<div class="info">
버튼을 누르고 있는 동안 움직이고
손을 떼면 정지합니다.
</div>
</div>
<div class="card neck-control-card neck-b-card">
<div class="neck-title">B · 목 하단 관절</div>
    <div class="neck-note">상·하 버튼으로 조작</div>
    <div class="neck-b-row">
        <button type="button" class="neck-b-btn" data-neck-axis="b_tilt" data-neck-dir="1">▲ 위</button>
        <button type="button" class="neck-b-btn" data-neck-axis="b_tilt" data-neck-dir="-1">▼ 아래</button>
    </div>
    <div class="neck-note">버튼을 누르고 있는 동안 움직이며 손을 떼면 정지합니다.</div>
</div>
<div class="card voice-card">
    <div class="card-title">🎤 한국어 음성 인식</div>
    <button type="button" id="piVoiceButton" class="voice-button">🎤 누르고 말하기</button>
    <div class="voice-hint">라즈베리파이에 연결한 ReSpeaker Lite로 녹음합니다.<br>누르고 있는 동안 녹음 · 놓으면 음성 인식</div>
    <div class="voice-text" id="piVoiceStatus">음성 인식 준비 상태 확인 중...</div>
    <div class="voice-text" id="piVoiceText"></div>
</div>
</div>
<div class="footer">
PiCar-Pro Mobile GUI
</div>
</div>

<!-- 카메라별 전체화면: 평소에는 숨김. 별도 Python 파일 불필요. -->
<div id="cameraFullscreenLayer" hidden aria-hidden="true" role="dialog" aria-label="카메라 전체화면">
  <div class="fs-video-backdrop">
    <img id="fullscreenMainImage" class="fs-main-image" alt="선택된 카메라 영상" />
    <div id="fullscreenNoFeed" class="fs-empty-image">카메라 영상 연결 대기</div>
  </div>
  <div class="fs-topbar">
    <span id="fullscreenCameraTitle" class="fs-camera-title">로봇 카메라</span>
    <div class="fs-top-actions">
      <button id="fullscreenStopButton" class="fs-action stop" type="button">■ STOP</button>
      <button id="fullscreenFlashButton" class="fs-action" type="button">후레시 ON</button>
      <button id="fullscreenCloseButton" class="fs-action exit" type="button" aria-label="전체화면 종료">✕ 닫기</button>
    </div>
  </div>
  <div class="fs-bottom-controls">
    <section class="fs-joy-unit" aria-label="주행 조이스틱">
      <span class="fs-joy-label">주행</span>
      <div id="fullscreenMoveJoystickHost" class="fs-joystick-holder"></div>
      <span id="fullscreenMoveStatus" class="fs-joy-status">정지</span>
    </section>
    <section class="fs-joy-unit" aria-label="목 A 조이스틱">
      <span class="fs-joy-label">목 A</span>
      <div id="fullscreenNeckJoystickHost" class="fs-joystick-holder"></div>
      <span id="fullscreenNeckStatus" class="fs-joy-status">목 A 정지</span>
    </section>
  </div>
</div>

<script>
let gripButton=null;
let joystickActive=false;
let joystickPointerId=null;
let joystickCommand= "stop_move";
let movementSequence=Date.now();
let gripChain=Promise.resolve();
const joystick=document.getElementById("joystick");
const joystickKnob=document.getElementById("joystickKnob");
const cameraImage=document.getElementById("robotCamera");
const cameraState=document.getElementById("cameraState");
const DEAD_ZONE=0.22;
/* ========================================================
   상태 표시
======================================================== */
function setConnected(connected){
    const text=document.getElementById("serverText");
    const dot=document.getElementById("serverDot");
    if (connected){
        text.textContent= "연결됨";
        dot.style.background= "#20a06b";
    }
    else{
        text.textContent= "연결 오류";
        dot.style.background= "#e34d59";
    }
}
/* ========================================================
   일반 명령
======================================================== */
async function sendCommand(command,keepalive=false){
    try{
        const response=await fetch("/api/command",{
            method:"POST",headers:{
                "Content-Type":"application/json"
            }
            ,body:JSON.stringify({
                command:command
            }
            ),cache:"no-store",keepalive:keepalive
        }
        );
        if (!response.ok){
            throw new Error();
        }
        setConnected(true);
        return true;
    }
    catch (_){
        setConnected(false);
        return false;
    }
}
/* ========================================================
   조이스틱 이동 명령
======================================================== */
function sendMoveCommand(command){
    movementSequence+=1;
    const seq=movementSequence;
    fetch("/api/command",{
        method:"POST",headers:{
            "Content-Type":"application/json"
        }
        ,body:JSON.stringify({
            command:command,seq:seq
        }
        ),cache:"no-store",keepalive:true
    }
    ).then(response=>{
        if (!response.ok){
            throw new Error();
        }
        setConnected(true);
    }
    ).catch(()=>{
        setConnected(false);
    }
    );
}
/* ========================================================
   집게 명령
======================================================== */
function queueGripCommand(command){
    gripChain=gripChain.then(()=>sendCommand(command,true));
    return gripChain;
}
function setMotion(text){
    document.getElementById("motion").textContent=text;
}
/* ========================================================
   조이스틱 방향 판정
======================================================== */
function getJoystickCommand(nx,ny){
    const distance=Math.sqrt(nx*nx+ny*ny);
    if (distance<DEAD_ZONE){
        return{
            command:"stop_move",label:"정지"
        }
        ;
    }
    const angle=Math.atan2(-ny,nx)*180/ Math.PI;
    if (angle>=-22.5&&angle<22.5){
        return{
            command:"right",label:"우회전"
        }
        ;
    }
    if (angle>=22.5&&angle<67.5){
        return{
            command:"right",label:"전진 우회전"
        }
        ;
    }
    if (angle>=67.5&&angle<112.5){
        return{
            command:"forward",label:"전진"
        }
        ;
    }
    if (angle>=112.5&&angle<157.5){
        return{
            command:"left",label:"전진 좌회전"
        }
        ;
    }
    if (angle>=157.5||angle<-157.5){
        return{
            command:"left",label:"좌회전"
        }
        ;
    }
    if (angle>=-157.5&&angle<-112.5){
        return{
            command:"backleft",label:"후진 좌회전"
        }
        ;
    }
    if (angle>=-112.5&&angle<-67.5){
        return{
            command:"backward",label:"후진"
        }
        ;
    }
    return{
        command:"backright",label:"후진 우회전"
    }
    ;
}
/* ========================================================
   조이스틱
======================================================== */
function updateJoystick(clientX,clientY){
    const rect=joystick.getBoundingClientRect();
    const centerX=rect.left+rect.width/ 2;
    const centerY=rect.top+rect.height/ 2;
    const maxRadius=rect.width*0.33;
    let dx=clientX-centerX;
    let dy=clientY-centerY;
    const distance=Math.sqrt(dx*dx+dy*dy);
    if (distance>maxRadius){
        const scale=maxRadius/ distance;
        dx*=scale;
        dy*=scale;
    }
    joystickKnob.style.transform= "translate("+ "calc(-50% + "+dx+ "px), "+ "calc(-50% + "+dy+ "px)"+ ")";
    const nx=dx/ maxRadius;
    const ny=dy/ maxRadius;
    const result=getJoystickCommand(nx,ny);
    setMotion(result.label);
    if (result.command!==joystickCommand){
        joystickCommand=result.command;
        sendMoveCommand(result.command);
    }
}
function resetJoystick(){
    joystickActive=false;
    joystickPointerId=null;
    joystickKnob.style.transform= "translate(-50%, -50%)";
    setMotion("정지");
    if (joystickCommand!== "stop_move"){
        joystickCommand= "stop_move";
        sendMoveCommand("stop_move");
    }
}
joystick.addEventListener("pointerdown",event=>{
    event.preventDefault();
    joystickActive=true;
    joystickPointerId=event.pointerId;
    try{
        joystick.setPointerCapture(event.pointerId);
    }
    catch (_){
    }
    updateJoystick(event.clientX,event.clientY);
}
);
joystick.addEventListener("pointermove",event=>{
    if (!joystickActive||event.pointerId!==joystickPointerId){
        return;
    }
    event.preventDefault();
    updateJoystick(event.clientX,event.clientY);
}
);
joystick.addEventListener("pointerup",event=>{
    if (event.pointerId===joystickPointerId){
        event.preventDefault();
        resetJoystick();
    }
}
);
joystick.addEventListener("pointercancel",event=>{
    if (event.pointerId===joystickPointerId){
        resetJoystick();
    }
}
);
/* ========================================================
   라이트
======================================================== */
document.getElementById("lightOn").addEventListener("click",async ()=>{
    const ok=await sendCommand("light_on");
    if (!ok){
        return;
    }
    document.getElementById("lightText").textContent= "현재: 양쪽 라이트 계속 ON";
    document.getElementById("lightOn").classList.add("light-selected");
    document.getElementById("lightOff").classList.remove("light-selected");
}
);
document.getElementById("lightOff").addEventListener("click",async ()=>{
    const ok=await sendCommand("light_off");
    if (!ok){
        return;
    }
    document.getElementById("lightText").textContent= "현재: 자동 모드";
    document.getElementById("lightOn").classList.remove("light-selected");
    document.getElementById("lightOff").classList.add("light-selected");
}
);
/* ========================================================
   집게
======================================================== */
document.querySelectorAll(".grip").forEach(button=>{
    button.addEventListener("pointerdown",event=>{
        event.preventDefault();
        if (gripButton&&gripButton!==button){
            gripButton.classList.remove("active");
        }
        gripButton=button;
        button.classList.add("active");
        try{
            button.setPointerCapture(event.pointerId);
        }
        catch (_){
        }
        queueGripCommand(button.dataset.command);
    }
    );
    const stopGrip=event=>{
        event.preventDefault();
        if (gripButton!==button){
            return;
        }
        button.classList.remove("active");
        gripButton=null;
        queueGripCommand("grip_stop");
    }
    ;
    button.addEventListener("pointerup",stopGrip);
    button.addEventListener("pointercancel",stopGrip);
}
);
/* ========================================================
   카메라 자동 복구
======================================================== */
let lastSeenFrameId=-1;
let lastFrameProgressTime=Date.now();
function reconnectCamera(){
    cameraState.textContent= "재연결 중";
    cameraImage.src= "/video_feed?t="+Date.now();
}
cameraImage.onerror=function (){
    reconnectCamera();
}
;
cameraImage.onload=function (){
    cameraState.textContent= "LIVE";
}
;
async function cameraWatchdog(){
    try{
        const response=await fetch("/api/status",{
            cache:"no-store"
        }
        );
        if (!response.ok){
            throw new Error();
        }
        const data=await response.json();
        setConnected(true);
        if (!data.camera_connected){
            cameraState.textContent= "카메라 재연결 중";
            return;
        }
        if (data.camera_frame_id!==lastSeenFrameId){
            lastSeenFrameId=data.camera_frame_id;
            lastFrameProgressTime=Date.now();
            cameraState.textContent= "LIVE";
            return;
        }
        if (Date.now()-lastFrameProgressTime>5000){
            lastFrameProgressTime=Date.now();
            reconnectCamera();
        }
    }
    catch (_){
        setConnected(false);
    }
}
setInterval(cameraWatchdog,2000);
/* ========================================================
   화면 이탈 안전정지
======================================================== */
window.addEventListener("blur",()=>{
    if (joystickActive){
        resetJoystick();
    }
    if (gripButton){
        gripButton.classList.remove("active");
        gripButton=null;
        queueGripCommand("grip_stop");
    }
}
);
document.addEventListener("visibilitychange",()=>{
    if (!document.hidden){
        return;
    }
    if (joystickActive){
        resetJoystick();
    }
    if (gripButton){
        gripButton.classList.remove("active");
        gripButton=null;
        queueGripCommand("grip_stop");
    }
}
);
window.addEventListener("pagehide",()=>{
    const moveData=new Blob([JSON.stringify({
        command:"stop_move",seq:movementSequence+1
    }
    )],{
        type:"application/json"
    }
    );
    navigator.sendBeacon("/api/command",moveData);
    const gripData=new Blob([JSON.stringify({
        command:"grip_stop"
    }
    )],{
        type:"application/json"
    }
    );
    navigator.sendBeacon("/api/command",gripData);
}
);
/* ========================================================
   상태
======================================================== */
async function loadStatus(){
    try{
        const response=await fetch("/api/status",{
            cache:"no-store"
        }
        );
        if (!response.ok){
            throw new Error();
        }
        const data=await response.json();
        setConnected(true);
        if (data.light_always_on){
            document.getElementById("lightText").textContent= "현재: 양쪽 라이트 계속 ON";
            document.getElementById("lightOn").classList.add("light-selected");
            document.getElementById("lightOff").classList.remove("light-selected");
        }
        else{
            document.getElementById("lightText").textContent= "현재: 자동 모드";
            document.getElementById("lightOn").classList.remove("light-selected");
            document.getElementById("lightOff").classList.add("light-selected");
        }
    }
    catch (_){
        setConnected(false);
    }
}
/* 카메라별 전체화면은 아래 fullscreen 전용 스크립트에서 처리합니다. */
loadStatus();
cameraWatchdog();

/* VOICE_REPEAKER_1: use SAME ORIGIN proxy, never browser microphone permission */
(function () {
    const btn = document.getElementById('piVoiceButton');
    if (!btn) return;
    const status = document.getElementById('piVoiceStatus');
    const output = document.getElementById('piVoiceText');
    let held = false;
    let sequence = Promise.resolve();
    let lastResult = '';

    function voiceAction(action) {
        // Start and stop are serialized to avoid reversed requests on slow Wi-Fi.
        sequence = sequence.catch(() => {}).then(async () => {
            const response = await fetch('/api/voice/' + action, {
                method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'},
                body:'{}', keepalive:true
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
        }).catch(err => {
            status.textContent = '마이크 명령 전송 오류: ' + err.message;
        });
    }
    function start(e) {
        if (e) e.preventDefault();
        if (held) return;
        held = true;
        btn.classList.add('recording');
        btn.textContent = '● 녹음 중 · 손을 떼면 인식';
        voiceAction('start');
    }
    const voiceHeartbeat = setInterval(() => {
        if (held) voiceAction('ping');
    }, 650);
    function end() {
        if (!held) return;
        held = false;
        btn.classList.remove('recording');
        btn.textContent = '🎤 누르고 말하기';
        voiceAction('stop');
    }
    btn.addEventListener('pointerdown', e => {
        start(e);
        try { btn.setPointerCapture(e.pointerId); } catch (_) {}
    });
    for (const ev of ['pointerup','pointercancel','lostpointercapture']) {
        btn.addEventListener(ev, end);
    }
    window.addEventListener('blur', end);
    window.addEventListener('pagehide', end);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) end();
    });

    async function refreshVoice() {
        try {
            const response = await fetch('/api/voice/status', {cache:'no-store'});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const data = await response.json();
            const names = {
                loading:'음성 모델 준비 중', ready:'음성 인식 준비 완료',
                opening:'마이크 연결 중', recording:'녹음 중',
                processing:'한국어 인식 중', done:'음성 인식 완료',
                error:'음성 오류', offline:'음성 서비스 오프라인'
            };
            status.textContent = names[data.state] || data.message || '연결 확인 중';
            if (data.state === 'done' && data.text && data.text !== lastResult) {
                lastResult = data.text;
                output.textContent = '인식 결과: ' + data.text +
                    (data.elapsed != null ? ' (' + data.elapsed + '초)' : '');
            }
            if (data.state === 'error' || data.state === 'offline') {
                output.textContent = data.error || data.message || '마이크 연결 확인 필요';
            }
        } catch (err) {
            status.textContent = '음성 서비스 연결 확인 중';
        }
    }
    refreshVoice();
    setInterval(refreshVoice, 650);
})();


/* SMOOTH_AB_INTEGRATION_v1 : neck joystick/button keepalive */
(function () {
    const j = document.getElementById('neckJoystick');
    if (!j) return;
    const k = document.getElementById('neckKnob');
    let seq = Date.now() * 1000;
    let heldPointer = null;
    let timer = null;
    let pan = 0;
    let tilt = 0;
    const bHolds = new Map();

    function neckSend(axis, value) {
        ++seq;
        fetch('/api/command', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command:'neck:' + axis + ':' + value, seq:seq}),
            cache: 'no-store', keepalive: true
        }).catch(() => {});
    }
    function readJoystick(event) {
        const r = j.getBoundingClientRect();
        const centerX = r.left + r.width / 2;
        const centerY = r.top + r.height / 2;
        const maxR = r.width * .31;
        let dx = event.clientX - centerX;
        let dy = event.clientY - centerY;
        const distance = Math.hypot(dx, dy);
        if (distance > maxR) { dx *= maxR / distance; dy *= maxR / distance; }
        k.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
        const x = dx / maxR, y = dy / maxR;
        const newPan = x > .25 ? 1 : x < -.25 ? -1 : 0;
        const newTilt = y < -.25 ? 1 : y > .25 ? -1 : 0;
        if (newPan !== pan) { pan = newPan; neckSend('a_pan', pan); }
        if (newTilt !== tilt) { tilt = newTilt; neckSend('a_tilt', tilt); }
    }
    function heartbeat() {
        if (heldPointer === null) return;
        if (pan) neckSend('a_pan', pan);
        if (tilt) neckSend('a_tilt', tilt);
    }
    function joystickStop() {
        if (timer !== null) clearInterval(timer);
        timer = null; heldPointer = null;
        pan = 0; tilt = 0;
        neckSend('a_pan', 0);
        neckSend('a_tilt', 0);
        k.style.transform = 'translate(-50%, -50%)';
    }
    j.addEventListener('pointerdown', e => {
        e.preventDefault();
        if (heldPointer !== null) return;
        heldPointer = e.pointerId;
        try { j.setPointerCapture(e.pointerId); } catch (_) {}
        readJoystick(e);
        timer = setInterval(heartbeat, 260);
    });
    j.addEventListener('pointermove', e => {
        if (heldPointer === e.pointerId) { e.preventDefault(); readJoystick(e); }
    });
    for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        j.addEventListener(event, () => { if (heldPointer !== null) joystickStop(); });
    }

    document.querySelectorAll('[data-neck-axis]').forEach(btn => {
        const axis = btn.dataset.neckAxis;
        const direction = Number(btn.dataset.neckDir);
        let id = null;
        let pointer = null;
        function endHold() {
            if (id !== null) clearInterval(id);
            id = null; pointer = null;
            bHolds.delete(btn);
            btn.classList.remove('active');
            neckSend(axis, 0);
        }
        btn.addEventListener('pointerdown', e => {
            e.preventDefault();
            if (id !== null) return;
            pointer = e.pointerId;
            btn.classList.add('active');
            try { btn.setPointerCapture(pointer); } catch (_) {}
            neckSend(axis, direction);
            id = setInterval(() => neckSend(axis, direction), 260);
            bHolds.set(btn, endHold);
        });
        for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
            btn.addEventListener(event, () => { if (id !== null) endHold(); });
        }
    });

    function stopAll() {
        if (heldPointer !== null) joystickStop();
        for (const stop of Array.from(bHolds.values())) stop();
    }
    window.addEventListener('blur', stopAll);
    window.addEventListener('pagehide', stopAll);
    document.addEventListener('visibilitychange', () => { if (document.hidden) stopAll(); });
})();


/* SMOOTH_AB_INTEGRATION_v1: grip keepalive without servo restart */
(function () {
    let gripRepeat = null;
    document.querySelectorAll('.grip').forEach(btn => {
        btn.addEventListener('pointerdown', () => {
            if (gripRepeat !== null) clearInterval(gripRepeat);
            gripRepeat = setInterval(() => {
                if (gripButton === btn) queueGripCommand(btn.dataset.command);
            }, 320);
        });
        function end() {
            if (gripRepeat !== null) clearInterval(gripRepeat);
            gripRepeat = null;
        }
        btn.addEventListener('pointerup', end);
        btn.addEventListener('pointercancel', end);
        btn.addEventListener('lostpointercapture', end);
        btn.addEventListener('pointerleave', end);
        window.addEventListener('blur', end);
        document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
    });
})();

</script>

<script>
/* 기존 조작·음성 스크립트는 위에서 실행됩니다.
   아래는 환경 영상/후레시 단축 버튼/목 A 상태 표시만 담당합니다. */
(function(){
  'use strict';
  const flash=document.getElementById('tabletFlash');
  const lightOn=document.getElementById('lightOn');
  const lightOff=document.getElementById('lightOff');
  const lightText=document.getElementById('lightText');
  function refreshFlash(){
    const active=lightOn.classList.contains('light-selected')||lightText.textContent.includes('계속 ON');
    flash.textContent=active?'후레시 OFF':'후레시 ON';
    flash.classList.toggle('light-selected',active);
  }
  flash.addEventListener('click',()=>{
    const active=flash.classList.contains('light-selected');
    (active?lightOff:lightOn).click();
  });
  new MutationObserver(refreshFlash).observe(lightText,{childList:true,subtree:true,characterData:true});
  refreshFlash();

  const status=document.getElementById('neckStatus');
  const joystick=document.getElementById('neckJoystick');
  let held=null;
  function update(e){
    const r=joystick.getBoundingClientRect(), mx=r.left+r.width/2,my=r.top+r.height/2;
    const x=(e.clientX-mx)/(r.width*.31),y=(e.clientY-my)/(r.width*.31);
    const pan=x>.25?'오른쪽':x<-.25?'왼쪽':'정지';
    const tilt=y<-.25?'위':y>.25?'아래':'정지';
    status.textContent='목 A · 좌우 '+pan+' / 상하 '+tilt;
  }
  function end(){held=null;status.textContent='목 A 정지';}
  joystick.addEventListener('pointerdown',e=>{if(held===null){held=e.pointerId;update(e);}});
  joystick.addEventListener('pointermove',e=>{if(held===e.pointerId)update(e);});
  ['pointerup','pointercancel','lostpointercapture'].forEach(evt=>joystick.addEventListener(evt,end));
  window.addEventListener('blur',end);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)end();});

  const btn=document.getElementById('environmentConfig');
  const img=document.getElementById('environmentCamera');
  const placeholder=document.getElementById('environmentPlaceholder');
  const envState=document.getElementById('environmentState');
  function setUrl(value){
    const raw=(value||'').trim();
    if(!raw){img.removeAttribute('src');img.style.display='none';placeholder.style.display='flex';envState.textContent='미연결';return;}
    let url;
    try{url=new URL(raw,location.href);if(!['http:','https:'].includes(url.protocol))throw Error('프로토콜');}
    catch(_){alert('http:// 또는 https:// 영상 주소를 입력하세요.');return;}
    img.style.display='block';placeholder.style.display='none';envState.textContent='연결 중';
    img.src=url.href;
    try{localStorage.setItem('picar_environment_camera',raw);}catch(_){}
  }
  img.addEventListener('load',()=>{envState.textContent='LIVE';});
  img.addEventListener('error',()=>{img.style.display='none';placeholder.style.display='flex';envState.textContent='연결 실패';});
  btn.addEventListener('click',()=>{
    let old='';try{old=localStorage.getItem('picar_environment_camera')||'';}catch(_){}
    const value=prompt('환경 카메라 MJPEG 또는 이미지 URL (삭제하려면 빈칸)',old);
    if(value!==null){if(!value.trim())try{localStorage.removeItem('picar_environment_camera');}catch(_){}setUrl(value);}
  });
  let saved='';try{saved=localStorage.getItem('picar_environment_camera')||'';}catch(_){}
  if(saved)setUrl(saved);
})();
</script>

<script>
/* ============================================================
   카메라별 전체화면: 로봇카메라 / 환경카메라
   기존 조이스틱 DOM을 화면에 옮겼다가 복원하므로 주행/목 A
   pointerdown / pointermove / pointerup 이벤트와 API는 그대로 작동.
   ============================================================ */
(function(){
  'use strict';
  const layer=document.getElementById('cameraFullscreenLayer');
  const image=document.getElementById('fullscreenMainImage');
  const empty=document.getElementById('fullscreenNoFeed');
  const title=document.getElementById('fullscreenCameraTitle');
  const moveHost=document.getElementById('fullscreenMoveJoystickHost');
  const neckHost=document.getElementById('fullscreenNeckJoystickHost');
  const moveJoy=document.getElementById('joystick');
  const neckJoy=document.getElementById('neckJoystick');
  const moveStatus=document.getElementById('motion');
  const neckStatus=document.getElementById('neckStatus');
  const fsMoveStatus=document.getElementById('fullscreenMoveStatus');
  const fsNeckStatus=document.getElementById('fullscreenNeckStatus');
  const flash=document.getElementById('fullscreenFlashButton');
  const close=document.getElementById('fullscreenCloseButton');
  const stop=document.getElementById('fullscreenStopButton');
  const originalBodyOverflow=document.body.style.overflow;
  // Keep real original parents and sibling positions, not newly cloned elements.
  const home=[
    {node:moveJoy,parent:moveJoy.parentNode,next:moveJoy.nextSibling,host:moveHost},
    {node:neckJoy,parent:neckJoy.parentNode,next:neckJoy.nextSibling,host:neckHost}
  ];
  let open=false;

  function syncStatuses(){
    fsMoveStatus.textContent=moveStatus.textContent || '정지';
    fsNeckStatus.textContent=neckStatus.textContent || '목 A 정지';
    const topFlash=document.getElementById('tabletFlash');
    flash.textContent=topFlash.textContent;
  }
  new MutationObserver(syncStatuses).observe(moveStatus,{childList:true,characterData:true,subtree:true});
  new MutationObserver(syncStatuses).observe(neckStatus,{childList:true,characterData:true,subtree:true});
  new MutationObserver(syncStatuses).observe(document.getElementById('tabletFlash'),{childList:true,characterData:true,subtree:true});
  syncStatuses();

  // Existing event handlers listen for blur to release pressed controls safely.
  function releaseHeldControls(){ window.dispatchEvent(new Event('blur')); }

  function openCamera(kind){
    if(open)return;
    releaseHeldControls();
    const source=kind==='environment' ? document.getElementById('environmentCamera') : document.getElementById('robotCamera');
    const connected=Boolean(source.getAttribute('src')) && source.style.display!=='none';
    title.textContent=kind==='environment'?'환경 카메라':'로봇 카메라';
    if(connected){
      // A separate image preserves the camera feed on the normal GUI.
      image.style.display='block';
      image.src=source.src;
      empty.style.display='none';
    } else {
      image.removeAttribute('src');
      image.style.display='none';
      empty.textContent=kind==='environment'?'환경 카메라 연결 대기 · 일반 화면의 ⚙에서 영상 주소를 설정하세요.':'로봇 카메라 연결 대기';
      empty.style.display='flex';
    }
    home.forEach(item=>item.host.appendChild(item.node));
    layer.hidden=false;
    layer.setAttribute('aria-hidden','false');
    document.body.style.overflow='hidden';
    open=true;
    syncStatuses();
    // requestFullscreen may not be supported (e.g., iOS). Fixed layer still works.
    if(typeof layer.requestFullscreen==='function'){
      try{const result=layer.requestFullscreen();if(result&&typeof result.catch==='function')result.catch(()=>{});}catch(_){}
    }
  }
  async function closeCamera(exitNative=true){
    if(!open)return;
    releaseHeldControls();
    // A stop on closing is intentional even if joystick release was missed.
    // Regular server JS owns the command dispatch; blur invokes its safety stop.
    home.forEach(item=>item.parent.insertBefore(item.node,item.next));
    open=false;
    layer.hidden=true;
    layer.setAttribute('aria-hidden','true');
    document.body.style.overflow=originalBodyOverflow;
    image.removeAttribute('src');
    image.style.display='none';
    if(exitNative&&document.fullscreenElement===layer&&typeof document.exitFullscreen==='function'){
      try{await document.exitFullscreen();}catch(_){}
    }
  }
  document.getElementById('fullscreenButton').addEventListener('click',()=>openCamera('robot'));
  document.getElementById('environmentFullscreenButton').addEventListener('click',()=>openCamera('environment'));
  close.addEventListener('click',()=>closeCamera());
  flash.addEventListener('click',()=>document.getElementById('tabletFlash').click());
  stop.addEventListener('click',()=>{
    releaseHeldControls();
    // In live server mode: send explicit safe stop if needed.
    if(typeof sendMoveCommand==='function')sendMoveCommand('stop_move');
  });
  image.addEventListener('error',()=>{
    if(!open)return;
    image.style.display='none';
    empty.textContent='선택한 카메라 영상을 불러올 수 없습니다.';
    empty.style.display='flex';
  });
  document.addEventListener('fullscreenchange',()=>{
    if(open && document.fullscreenElement!==layer)closeCamera(false);
  });
  document.addEventListener('keydown',e=>{
    if(open&&e.key==='Escape'){e.preventDefault();closeCamera();}
  });
  window.addEventListener('pagehide',()=>{if(open)closeCamera(false);});
})();
</script>
</body>
</html>
"""# =========================================================
# HTTP Handler

# =========================================================

class MobileHandler(
    BaseHTTPRequestHandler
):
    protocol_version = "HTTP/1.1"
    def log_message(
        self,
        format,
        *args
    ):
        return
    def send_json(
        self,
        data,
        status=200
    ):
        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode(
            "utf-8"
        )
        self.send_response(
            status
        )
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(
                len(body)
            )
        )
        self.send_header(
            "Cache-Control",
            "no-store"
        )
        self.end_headers()
        self.wfile.write(
            body
        )
    def do_GET(
        self
    ):
        if self.path.split('?', 1)[0] == '/api/voice/status':
            self.send_json(voice_request('status'))
            return
        if (
            self.path == "/"
            or
            self.path.startswith(
                "/?"
            )
        ):
            body = (
                MOBILE_HTML
                .encode(
                    "utf-8"
                )
            )
            self.send_response(
                200
            )
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(
                    len(body)
                )
            )
            self.send_header(
                "Cache-Control",
                "no-store, no-cache, must-revalidate"
            )
            self.end_headers()
            self.wfile.write(
                body
            )
            return
        if self.path.startswith(
            "/api/status"
        ):
            with control_lock:
                data = {
                    "status":
                        "ok",
                    "motion":
                        current_motion,
                    "light_always_on":
                        light_always_on,
                    "camera_connected":
                        camera_connected,
                    "camera_frame_id":
                        latest_frame_id,
                    "camera_error":
                        camera_last_error
                }
            self.send_json(
                data
            )
            return
        if self.path.startswith(
            "/video_feed"
        ):
            self.send_response(
                200
            )
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame"
            )
            self.send_header(
                "Cache-Control",
                "no-cache, no-store, must-revalidate"
            )
            self.send_header(
                "Pragma",
                "no-cache"
            )
            self.send_header(
                "Expires",
                "0"
            )
            self.end_headers()
            last_id = -1
            try:
                while server_running:
                    with camera_condition:
                        camera_condition.wait_for(
                            lambda:
                                (
                                    latest_frame_id
                                    !=
                                    last_id
                                )
                                or
                                (
                                    not
                                    server_running
                                ),
                            timeout=2.0
                        )
                        if not server_running:
                            break
                        jpeg = latest_jpeg
                        frame_id = latest_frame_id
                    if jpeg is None:
                        continue
                    if frame_id == last_id:
                        continue
                    last_id = frame_id
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        +
                        (
                            f"Content-Length: "
                            f"{len(jpeg)}"
                            f"\r\n\r\n"
                        ).encode()
                    )
                    self.wfile.write(
                        header
                    )
                    self.wfile.write(
                        jpeg
                    )
                    self.wfile.write(
                        b"\r\n"
                    )
                    self.wfile.flush()
            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError
            ):
                print(
                    "[카메라] 브라우저 영상 연결 종료"
                )
            except Exception as e:
                print(
                    "[영상 스트림 오류]",
                    repr(e)
                )
            return
        self.send_error(
            404
        )
    def do_POST(
        self
    ):
        if self.path.split('?', 1)[0] in ('/api/voice/start', '/api/voice/stop', '/api/voice/ping'):
            origin = self.headers.get('Origin')
            local_origin = 'http://' + self.headers.get('Host', '')
            if origin and origin != local_origin:
                self.send_json({'state': 'error', 'error': '잘못된 접속 출처'}, status=403)
                return
            action = self.path.rsplit('/', 1)[-1]
            self.send_json(voice_request(action))
            return
        global last_move_seq
        if not self.path.startswith(
            "/api/command"
        ):
            self.send_error(
                404
            )
            return
        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )
            raw = self.rfile.read(
                length
            )
            data = json.loads(
                raw.decode(
                    "utf-8"
                )
            )
            command = str(
                data.get(
                    "command",
                    ""
                )
            ).strip()
            if not command:
                self.send_json(
                    {
                        "status":
                            "error",
                        "message":
                            "명령 없음"
                    },
                    status=400
                )
                return
            movement_commands = {
                "forward",
                "backward",
                "left",
                "right",
                "backleft",
                "backright",
                "stop_move"
            }
            if command in movement_commands:
                seq_value = (
                    data.get(
                        "seq"
                    )
                )
                if seq_value is not None:
                    seq_value = int(
                        seq_value
                    )
                    with move_seq_lock:
                        if (
                            seq_value
                            <=
                            last_move_seq
                        ):
                            self.send_json(
                                {
                                    "status":
                                        "ignored",
                                    "command":
                                        command,
                                    "seq":
                                        seq_value
                                }
                            )
                            return
                        last_move_seq = (
                            seq_value
                        )
            print(
                "[모바일 명령]",
                command
            )
            robot_ctrl(command, data.get("seq"))
            self.send_json(
                {
                    "status":
                        "ok",
                    "command":
                        command,
                    "motion":
                        current_motion,
                    "light_always_on":
                        light_always_on
                }
            )
        except Exception as e:
            print(
                "[모바일 명령 오류]",
                repr(e)
            )
            self.send_json(
                {
                    "status":
                        "error",
                    "message":
                        str(e)
                },
                status=500
            )

# =========================================================
# HTTP Server

# =========================================================

class MobileHTTPServer(
    ThreadingHTTPServer
):
    allow_reuse_address = True
    daemon_threads = True

# =========================================================
# MAIN

# =========================================================

def main():
    global server_running
    global yolo_detector
    global async_yolo
    global oled_eyes
    print(
        "=========================================="
    )
    print(
        " PiCar-Pro Mobile Web Server"
    )
    print(
        "=========================================="
    )
    print(
        f"WEB PORT     : "
        f"{WEB_PORT}"
    )
    print(
        f"DRIVE SPEED  : "
        f"{DRIVE_SPEED}"
    )
    print(
        f"TURN SPEED   : "
        f"{TURN_SPEED}"
    )
    print(
        f"CAMERA       : "
        f"{CAMERA_WIDTH}x"
        f"{CAMERA_HEIGHT} "
        f"{CAMERA_FPS}FPS"
    )
    print(
        f"CAMERA SERVO : "
        "A/B 목 관절 제어"
    )
    print(
        f"YOLO         : "
        f"{YOLO_MODEL_PATH}, "
        f"imgsz={YOLO_IMAGE_SIZE}, "
        f"every={YOLO_DETECT_EVERY_N_FRAMES}"
    )
    print(
        "MOVE GUI     : 원형 조이스틱"
    )
    print(
        "GRIPPER      : 추가 delay 제거"
    )
    print(
        "CAMERA RETRY : 자동 재연결"
    )
    print(
        "=========================================="
    )
    # OLED
    oled_eyes = OLEDEyes(
        port=1,
        address=0x3C
    )
    if oled_eyes.connected:
        oled_eyes.start()
        oled_set_direction(
            "stop"
        )
    # YOLO
    try:
        yolo_detector = YOLODetector(
            model_path=
                YOLO_MODEL_PATH,
            confidence=
                YOLO_CONFIDENCE,
            imgsz=
                YOLO_IMAGE_SIZE,
            # 서버에서 8프레임마다 전달하므로 내부 주기는 1
            detect_every_n_frames=
                1
        )
        async_yolo = AsyncYOLO(
            yolo_detector
        )
        async_yolo.start()
    except Exception as e:
        yolo_detector = None
        async_yolo = None
        print(
            "[YOLO] 초기화 실패:",
            repr(e)
        )
        print(
            "[YOLO] 객체 탐지 없이 "
            "카메라만 실행합니다."
        )
    # 모터
    move.setup()
    # 라이트
    try:
        switch.switchSetup()
        switch.set_all_switch_off()
    except Exception as e:
        print(
            "[라이트 초기화 경고]",
            e
        )
    # Servo
    initialize_servos()
    global neck_controller, grip_controller
    neck_controller = SmoothNeck()
    neck_controller.start()
    grip_controller = SmoothGripper(gripper_servo, speed=GRIPPER_SPEED)
    # 시작 시 안전 정지
    safe_stop(
        force_lights_off=True
    )
    # 카메라 Thread
    threading.Thread(
        target=camera_worker,
        daemon=True,
        name="CameraThread"
    ).start()
    # HTTP 서버
    server = MobileHTTPServer(
        (
            HOST,
            WEB_PORT
        ),
        MobileHandler
    )
    print()
    print(
        f"[모바일 주소] "
        f"http://<라즈베리파이-IP>:"
        f"{WEB_PORT}"
    )
    print(
        "[서버 종료] Ctrl + C"
    )
    print()
    try:
        server.serve_forever(
            poll_interval=0.5
        )
    except KeyboardInterrupt:
        print(
            "\n[서버] 종료 요청"
        )
    finally:
        server_running = False
        with camera_condition:
            camera_condition.notify_all()
        if neck_controller is not None:
            neck_controller.close()
        if grip_controller is not None:
            grip_controller.close()
        gripper_stop()
        safe_stop(
            force_lights_off=True
        )
        if async_yolo is not None:
            async_yolo.stop()
        try:
            server.server_close()
        except Exception:
            pass
        try:
            move.destroy()
        except Exception:
            pass
        try:
            switch.set_all_switch_off()
            switch.switchClose()
        except Exception:
            pass
        try:
            if oled_eyes is not None:
                oled_eyes.set_direction(
                    "stop"
                )
                time.sleep(
                    0.1
                )
                oled_eyes.stop()
                if oled_eyes.is_alive():
                    oled_eyes.join(
                        timeout=1.0
                    )
        except Exception:
            pass
        print(
            "[서버] 종료 완료"
        )
if __name__ == "__main__":
    start_voice()  # VOICE_REPEAKER_1
    main()