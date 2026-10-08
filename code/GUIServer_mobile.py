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
import Switch as switch
from OLED_eyes import OLEDEyes
from YOLO_detector import YOLODetector

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
YOLO_MODEL_PATH = "yolov8n.pt"
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
    camera_servo.set_angle(1, CAMERA_FIXED_ANGLE)
    # 이전 버전의 setDelay(0.12) 제거
    # RPIservo 기본 delay를 그대로 사용
    gripper_servo.start()
    print(f"[카메라 목] Servo 1 = {CAMERA_FIXED_ANGLE}도 고정")
    print("[집게] 추가 delay 없이 기본 속도로 동작")

# =========================================================
# 집게

# =========================================================

def gripper_start(direction):
    # 기존:
    # stopWiggle()
    # sleep(0.02)
    # singleServo()
    #
    # 위 과정 때문에 버튼을 누를 때마다 끊겼다가
    # 다시 움직이는 느낌이 생길 수 있어 바로 방향만 전달한다.
    with gripper_lock:
        gripper_servo.singleServo(
            4,
            direction,
            GRIPPER_SPEED
        )

def gripper_stop():
    with gripper_lock:
        try:
            gripper_servo.stopWiggle()
        except Exception:
            pass

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

def robot_ctrl(command):
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
</style>
</head>
<body>
<div class="app">
<div class="header">
<div>
<div class="title">
PiCar-Pro
</div>
<div class="subtitle">
Mobile Control
</div>
</div>
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
<div class="footer">
PiCar-Pro Mobile GUI
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
/* ========================================================
   전체화면
======================================================== */
const appRoot=document.querySelector(".app");
const fullscreenButton=document.getElementById("fullscreenButton");
async function enterCameraFullscreen(){
    appRoot.classList.add("camera-fullscreen");
    document.body.style.overflow= "hidden";
    fullscreenButton.textContent= "✕";
    try{
        if (appRoot.requestFullscreen&&!document.fullscreenElement){
            await appRoot.requestFullscreen();
        }
    }
    catch (_){
    }
}
async function exitCameraFullscreen(){
    appRoot.classList.remove("camera-fullscreen");
    document.body.style.overflow= "";
    fullscreenButton.textContent= "⛶";
    try{
        if (document.fullscreenElement){
            await document.exitFullscreen();
        }
    }
    catch (_){
    }
}
fullscreenButton.addEventListener("click",()=>{
    if (appRoot.classList.contains("camera-fullscreen")){
        exitCameraFullscreen();
    }
    else{
        enterCameraFullscreen();
    }
}
);
document.addEventListener("fullscreenchange",()=>{
    if (!document.fullscreenElement&&appRoot.classList.contains("camera-fullscreen")){
        appRoot.classList.remove("camera-fullscreen");
        document.body.style.overflow= "";
        fullscreenButton.textContent= "⛶";
    }
}
);
loadStatus();
cameraWatchdog();
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
            robot_ctrl(
                command
            )
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
        f"{CAMERA_FIXED_ANGLE}도 고정"
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
    main()
