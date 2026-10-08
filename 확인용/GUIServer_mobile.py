#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""

=========================================================

파일 이름 : GUIServer_mobile.py

용도      : PiCar-Pro 핸드폰 전용 웹 GUI 서버

=========================================================

실행:

    cd ~/Adeept_PiCar-Pro/Server

    sudo python3 GUIServer_mobile.py

핸드폰 접속:

    http://라즈베리파이IP:5000

예:

    http://192.168.25.112:5000

주의:

- PC용 GUIServer_custom.py와 동시에 실행하지 않는다.

- PC GUI를 사용할 때는 GUIServer_custom.py 사용

- 모바일 GUI를 사용할 때는 GUIServer_mobile.py 사용

"""

import json
import socket
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

from picamera2 import Picamera2

import Move as move
import RPIservo
import Switch as switch

from OLED_eyes import OLEDEyes
from YOLO_detector import YOLODetector

# =========================================================

# OLED 눈동자 모듈

# =========================================================

# OLED_eyes.py에서 실제 OLED 그림/애니메이션을 담당한다.
# 이 서버는 현재 이동 방향만 전달한다.
oled_eyes = None

# YOLO 객체 탐지 모듈
yolo_detector = None

def oled_set_direction(direction):
    if oled_eyes is None:
        return

    try:
        oled_eyes.set_direction(direction)
    except Exception as e:
        print("[OLED] 방향 전달 오류:", e)

# =========================================================

# 주행 설정

# =========================================================

DRIVE_SPEED = 60

TURN_SPEED = 50

TURN_ANGLE = 30

# Adeept 공식 방향값

Dv = -1

# =========================================================

# 카메라 목 고정 각도

# =========================================================

# Servo 1 = 카메라 목

# 프로그램이 시작되면 이 위치로 한 번 이동한 뒤
# 이후에는 카메라 목을 움직이지 않는다.

# 카메라가 바닥을 보면:

# 90 -> 80 -> 70
# 으로 테스트.

# 반대로 움직이면:

# 90 -> 100 -> 110
# 으로 테스트.

# =========================================================

CAMERA_FIXED_ANGLE = 90

# =========================================================

# 카메라 영상 설정

# =========================================================

CAMERA_WIDTH = 640

CAMERA_HEIGHT = 480

CAMERA_FPS = 15

JPEG_QUALITY = 70

# =========================================================

# YOLO 객체 탐지 설정

# =========================================================

YOLO_MODEL_PATH = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35
YOLO_IMAGE_SIZE = 320
YOLO_DETECT_EVERY_N_FRAMES = 3

# =========================================================

# 프로그램 상태

# =========================================================

server_running = True

# LIGHT ON 여부

light_always_on = False

# 현재 이동 방향

current_motion = "stop"

# PC/핸드폰 요청이 겹쳐도 하드웨어 명령을
# 한 번에 하나씩 처리하기 위한 Lock

control_lock = threading.RLock()

# =========================================================

# 카메라 공유 데이터

# =========================================================

latest_jpeg = None

latest_frame_id = 0

# 새 프레임이 들어왔다는 것을
# 영상 전송 Thread에 알려주기 위해 사용

camera_condition = threading.Condition()

# =========================================================

# Servo 객체

# =========================================================

# Servo 0

# 앞바퀴 조향

steering = RPIservo.ServoCtrl()

# Servo 1

# 카메라 목

camera_servo = RPIservo.ServoCtrl()

# Servo 4

# 집게

gripper_servo = RPIservo.ServoCtrl()

# =========================================================

# Servo 초기화

# =========================================================

def initialize_servos():

    # -----------------------------------------------------

    # 앞바퀴 조향

    # RPIservo.py의 init_pwm0 값을 중앙으로 사용한다.

    # 현재
    # init_pwm0 = 60
    # 으로 맞춰놓은 상태.

    # -----------------------------------------------------

    steering.moveServoInit([0])

    # -----------------------------------------------------

    # 카메라 목

    # 시작할 때 한 번만 정면 위치로 이동시킨다.

    # 여기서는 moveInit()을 사용하지 않는다.

    # moveInit()을 사용하면 다른 Servo까지 같이

    # 초기화될 수 있기 때문이다.

    # -----------------------------------------------------

    camera_servo.set_angle(
        1,
        CAMERA_FIXED_ANGLE

    )

    # -----------------------------------------------------

    # 집게 제어 Thread

    # -----------------------------------------------------

    gripper_servo.start()

    print(

        f"[카메라 목] Servo 1 = "
        f"{CAMERA_FIXED_ANGLE}도 고정"

    )

# =========================================================

# 라이트 기본 함수

# =========================================================

def both_lights_on():

    switch.switch(
        1,
        1
    )

    switch.switch(
        2,
        1
    )

def both_lights_off():

    switch.switch(
        1,
        0
    )

    switch.switch(
        2,
        0
    )

# =========================================================

# 현재 움직임에 따라 라이트 적용

# =========================================================

def apply_motion_lights(

    motion=None

):

    if motion is None:

        motion = current_motion

    # =====================================================

    # LIGHT ON 모드

    # 이동 방향에 관계없이

    # 두 라이트를 계속 켜둔다.

    # =====================================================

    if light_always_on:

        both_lights_on()

        return

    # =====================================================

    # 자동 모드

    # =====================================================

    # 전진 / 후진

    # -> 양쪽 ON

    if motion in (

        "forward",

        "backward"

    ):

        both_lights_on()

    # 왼쪽

    # -> 왼쪽만 ON

    elif motion in (

        "left",

        "backleft"

    ):

        switch.switch(
            1,
            1
        )

        switch.switch(
            2,
            0
        )

    # 오른쪽

    # -> 오른쪽만 ON

    elif motion in (
        "right",
        "backright"
    ):

        switch.switch(
            1,
            0
        )

        switch.switch(
            2,
            1
        )

    # 정지
    # -> OFF

    else:
        both_lights_off()

# =========================================================

# LIGHT ON

# =========================================================

def light_on_mode():

    global light_always_on

    light_always_on = True

    both_lights_on()

    print(

        "[라이트] 항상 ON"

    )

# =========================================================

# LIGHT OFF

# 실제 의미:

# 자동 라이트 모드로 복귀

# =========================================================

def light_off_mode():

    global light_always_on

    light_always_on = False

    apply_motion_lights(

        current_motion

    )

    print(

        "[라이트] 자동 모드"

    )

# =========================================================

# 안전 정지

# =========================================================

def safe_stop(

    force_lights_off=False

):

    global current_motion

    with control_lock:

        # -------------------------------------------------

        # 모터 정지

        # -------------------------------------------------

        try:
            move.motorStop()

        except Exception:
            pass

        # -------------------------------------------------

        # 앞바퀴 중앙

        # moveAngle(0, 0)은

        # 실제 0도가 아니라 init_pwm0 기준 중앙이다.

        # init_pwm0 = 60이면

        # 실제 60도로 돌아간다.

        # -------------------------------------------------

        try:

            steering.moveAngle(
                0,
                0
            )

        except Exception:

            pass

        current_motion = "stop"

        oled_set_direction("stop")

        # -------------------------------------------------

        # 라이트

        # -------------------------------------------------

        try:

            if force_lights_off:
                both_lights_off()

            else:
                apply_motion_lights(
                    "stop"
                )

        except Exception:

            pass

# =========================================================

# 로봇 명령 처리

# =========================================================

def robot_ctrl(

    command

):

    global current_motion

    with control_lock:

        # =================================================

        # 전진

        # =================================================

        if command == "forward":

            current_motion = "forward"

            oled_set_direction("forward")

            steering.moveAngle(

                0,

                0

            )

            move.move(

                DRIVE_SPEED,

                1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 후진

        # =================================================

        elif command == "backward":

            current_motion = "backward"

            oled_set_direction("backward")

            steering.moveAngle(

                0,

                0

            )

            move.move(

                DRIVE_SPEED,

                -1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 전진 좌회전

        # =================================================

        elif command == "left":

            current_motion = "left"

            oled_set_direction("left")

            steering.moveAngle(

                0,

                TURN_ANGLE * Dv

            )

            time.sleep(

                0.05

            )

            move.move(

                TURN_SPEED,

                1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 전진 우회전

        # =================================================

        elif command == "right":

            current_motion = "right"

            oled_set_direction("right")

            steering.moveAngle(

                0,

                -TURN_ANGLE * Dv

            )

            time.sleep(

                0.05

            )

            move.move(

                TURN_SPEED,

                1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 후진 좌회전

        # =================================================

        elif command == "backleft":

            current_motion = "backleft"

            oled_set_direction("backleft")

            steering.moveAngle(

                0,

                TURN_ANGLE * Dv

            )

            time.sleep(

                0.05

            )

            move.move(

                TURN_SPEED,

                -1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 후진 우회전

        # =================================================

        elif command == "backright":

            current_motion = "backright"

            oled_set_direction("backright")

            steering.moveAngle(

                0,

                -TURN_ANGLE * Dv

            )

            time.sleep(

                0.05

            )

            move.move(

                TURN_SPEED,

                -1,

                "mid"

            )

            apply_motion_lights(

                current_motion

            )

        # =================================================

        # 이동 정지

        # =================================================

        elif command == "stop_move":

            safe_stop(

                force_lights_off=False

            )

        # =================================================

        # LIGHT ON

        # =================================================

        elif command == "light_on":

            light_on_mode()

        # =================================================

        # LIGHT OFF

        #

        # 자동 모드로 돌아감

        # =================================================

        elif command == "light_off":

            light_off_mode()

        # =================================================

        # 집게 잡기

        # =================================================

        elif command == "grab":

            gripper_servo.singleServo(

                4,

                -1,

                5

            )

        # =================================================

        # 집게 놓기

        # =================================================

        elif command == "loose":

            gripper_servo.singleServo(

                4,

                1,

                5

            )

        # =================================================

        # 집게 정지

        # =================================================

        elif command == "grip_stop":

            gripper_servo.stopWiggle()

        # =================================================

        # 카메라 Servo

        #

        # 지금은 어떤 명령이 들어와도 움직이지 않는다.

        # 센서 추가 후 이 부분에 추적 기능을 넣으면 됨.

        # =================================================

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

# 카메라 캡처 Thread

# =========================================================

def camera_worker():

    global latest_jpeg

    global yolo_detector

    global latest_frame_id

    camera = None

    try:

        # -------------------------------------------------

        # Picamera2 생성

        # -------------------------------------------------

        camera = Picamera2()

        # -------------------------------------------------

        # 영상용 configuration

        #

        # preview가 아니라 video configuration 사용

        # -------------------------------------------------

        # 기존에 정상 동작하던 모바일 카메라 설정을 그대로 사용한다.
        # FrameRate 제어를 강제로 넣으면 일부 libcamera 환경에서
        # FrameDurationLimits 오류가 발생하므로 사용하지 않는다.
        config = camera.create_preview_configuration(
            main={
                "format": "RGB888",
                "size": (CAMERA_WIDTH, CAMERA_HEIGHT),
            }
        )

        camera.configure(

            config

        )

        camera.start()

        # 카메라가 안정화될 시간

        time.sleep(

            1.0

        )

        print(

            "[카메라] 실시간 영상 시작"

        )

        # -------------------------------------------------

        # 계속 프레임 읽기

        # -------------------------------------------------

        while server_running:

            frame = (

                camera.capture_array()

            )

            if frame is None:

                time.sleep(

                    0.01

                )

                continue

            # -------------------------------------------------

            # Picamera2 RGB -> OpenCV BGR

            # -------------------------------------------------

            frame_bgr = cv2.cvtColor(

                frame,

                cv2.COLOR_RGB2BGR

            )

            # -------------------------------------------------
            # YOLO 객체 탐지 + 사각형 표시
            # -------------------------------------------------

            if yolo_detector is not None:
                frame_bgr = yolo_detector.detect_and_draw(
                    frame_bgr
                )

            # -------------------------------------------------

            # JPEG 압축

            # -------------------------------------------------

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

            if not ok:

                continue

            jpeg = (

                encoded.tobytes()

            )

            # -------------------------------------------------

            # 새 프레임 저장

            # -------------------------------------------------

            with camera_condition:

                latest_jpeg = jpeg

                latest_frame_id += 1

                # video_feed Thread에

                # 새 프레임이 들어왔다고 알림

                camera_condition.notify_all()

    except Exception as e:

        print(

            "[카메라 오류]",

            repr(e)

        )

    finally:

        # -------------------------------------------------

        # 카메라 종료

        # -------------------------------------------------

        if camera is not None:

            try:

                camera.stop()

            except Exception:

                pass

            try:

                camera.close()

            except Exception:

                pass

        print(

            "[카메라] 종료"

        )

# =========================================================

# 모바일 웹 페이지

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

<title>

PiCar-Pro Mobile

</title>

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

    --red: #e34d59;

    --red-light: #fdecee;

    --camera-bg: #171a1f;

}

* {

    box-sizing: border-box;

    -webkit-tap-highlight-color:

        transparent;

}

html,

body {

    margin: 0;

    background:

        var(--bg);

    color:

        var(--text);

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

    justify-content:

        space-between;

    align-items:

        center;

    gap: 10px;

    margin-bottom: 14px;

}

.title {

    font-size: 23px;

    font-weight: 800;

}

.subtitle {

    margin-top: 3px;

    color:

        var(--sub);

    font-size: 12px;

}

.server {

    flex-shrink: 0;

    padding:

        8px 11px;

    background:

        var(--white);

    border:

        1px solid

        var(--border);

    border-radius:

        12px;

    font-size: 12px;

}

.dot {

    display:

        inline-block;

    width: 8px;

    height: 8px;

    margin-right: 5px;

    background:

        var(--green);

    border-radius:

        50%;

}

.card {

    margin-bottom: 12px;

    padding: 14px;

    background:

        var(--white);

    border:

        1px solid

        var(--border);

    border-radius:

        18px;

}

.card-title {

    margin-bottom: 10px;

    font-size: 15px;

    font-weight: 800;

}

.camera-box {

    position: relative;

    width: 100%;

    aspect-ratio: 4 / 3;

    overflow: hidden;

    background:

        var(--camera-bg);

    border-radius: 13px;

}

.camera {

    display: block;

    width: 100%;

    height: 100%;

    object-fit: cover;

    background:

        var(--camera-bg);

}

.camera-state {

    position: absolute;

    left: 10px;

    bottom: 10px;

    padding:

        5px 8px;

    color: white;

    background:

        rgba(0, 0, 0, 0.5);

    border-radius: 8px;

    font-size: 11px;

}

.motion {

    min-height: 24px;

    margin-bottom: 9px;

    text-align: center;

    color:

        var(--sub);

    font-weight: 700;

}

.pad {

    display: grid;

    grid-template-columns:

        repeat(

            3,

            1fr

        );

    gap: 9px;

}

.row2 {

    display: grid;

    grid-template-columns:

        1fr 1fr;

    gap: 9px;

}

.btn {

    min-height: 68px;

    padding: 8px;

    border:

        1px solid

        var(--border);

    border-radius: 15px;

    background:

        var(--white);

    color:

        var(--text);

    font-size: 15px;

    font-weight: 800;

    touch-action: none;

    user-select: none;

    -webkit-user-select: none;

}

.btn.active {

    background:

        var(--blue);

    color:

        white;

    border-color:

        var(--blue);

}

.stop {

    background:

        var(--red-light);

    color:

        var(--red);

    border-color:

        transparent;

}

.light-on {

    background:

        var(--green-light);

    color:

        var(--green);

}

.light-off {

    background:

        var(--blue-light);

    color:

        var(--blue);

}

.light-selected {

    background:

        var(--green) !important;

    color:

        white !important;

}

.grab {

    background:

        var(--green-light);

    color:

        var(--green);

}

.loose {

    background:

        var(--blue-light);

    color:

        var(--blue);

}

.info {

    margin-top: 9px;

    color:

        var(--sub);

    font-size: 11px;

    line-height: 1.5;

}

.footer {

    padding:

        2px 4px 10px;

    text-align: center;

    color:

        var(--sub);

    font-size: 10px;

}

/* ======================================================
   카메라 전체화면
   - 가로: 카메라 왼쪽 / 조작부 오른쪽
   - 세로: 카메라 위 / 조작부 아래
====================================================== */

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

.app.camera-fullscreen {
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
    grid-template-columns: minmax(0, 1fr) 330px;
    grid-template-rows: auto auto 1fr;
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

.app.camera-fullscreen .camera-card .fullscreen-button {
    background: #20252d;
    color: white;
    border-color: #353c47;
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
    overflow-y: auto;
}

@media (orientation: portrait), (max-width: 700px) {
    .app.camera-fullscreen {
        overflow-y: auto;
        grid-template-columns: 1fr;
        grid-template-rows: 55vh auto auto auto;
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
        overflow: visible;
    }
}

</style>

</head>

<body>

<div class="app">

<!-- =====================================================

     HEADER

===================================================== -->

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

<!-- =====================================================

     CAMERA

===================================================== -->

<div class="card camera-card">

    <div class="card-title-row">

        <div class="card-title">
            로봇 카메라
        </div>

        <button
            id="fullscreenButton"
            class="fullscreen-button"
            type="button"
            aria-label="카메라 전체화면"
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

            LIVE

        </div>

    </div>

</div>

<!-- =====================================================

     MOVE

===================================================== -->

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

    <div class="pad">

        <div></div>

        <button

            class="btn move"

            data-command="forward"

            data-name="전진"

        >

            ▲

            <br>

            전진

        </button>

        <div></div>

        <button

            class="btn move"

            data-command="left"

            data-name="전진 좌회전"

        >

            ◀

            <br>

            좌회전

        </button>

        <button

            id="stopButton"

            class="btn stop"

        >

            ■

            <br>

            STOP

        </button>

        <button

            class="btn move"

            data-command="right"

            data-name="전진 우회전"

        >

            ▶

            <br>

            우회전

        </button>

        <button

            class="btn move"

            data-command="backleft"

            data-name="후진 좌회전"

        >

            ↙

            <br>

            후진 좌

        </button>

        <button

            class="btn move"

            data-command="backward"

            data-name="후진"

        >

            ▼

            <br>

            후진

        </button>

        <button

            class="btn move"

            data-command="backright"

            data-name="후진 우회전"

        >

            ↘

            <br>

            후진 우

        </button>

    </div>

</div>

<!-- =====================================================

     LIGHT

===================================================== -->

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

<!-- =====================================================

     GRIPPER

===================================================== -->

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

let movingButton = null;

// ========================================================

// 서버 상태

// ========================================================

function setConnected(

    connected

) {

    const text =

        document.getElementById(

            "serverText"

        );

    const dot =

        document.getElementById(

            "serverDot"

        );

    if (connected) {

        text.textContent =

            "연결됨";

        dot.style.background =

            "#20a06b";

    }

    else {

        text.textContent =

            "연결 오류";

        dot.style.background =

            "#e34d59";

    }

}

// ========================================================

// 서버 명령

// ========================================================

async function sendCommand(

    command,

    keepalive=false

) {

    try {

        const response =

            await fetch(

                "/api/command",

                {

                    method:

                        "POST",

                    headers: {

                        "Content-Type":

                            "application/json"

                    },

                    body:

                        JSON.stringify({

                            command:

                                command

                        }),

                    cache:

                        "no-store",

                    keepalive:

                        keepalive

                }

            );

        if (!response.ok) {

            throw new Error(

                "HTTP ERROR"

            );

        }

        setConnected(

            true

        );

        return true;

    }

    catch (error) {

        setConnected(

            false

        );

        return false;

    }

}

// ========================================================

// 이동 상태 표시

// ========================================================

function setMotion(

    text

) {

    document

        .getElementById(

            "motion"

        )

        .textContent =

            text;

}

// ========================================================

// 안전 정지

// ========================================================

async function stopMovement() {

    if (movingButton) {

        movingButton

            .classList

            .remove(

                "active"

            );

        movingButton = null;

    }

    setMotion(

        "정지"

    );

    await sendCommand(

        "stop_move",

        true

    );

}

// ========================================================

// 이동 버튼

// ========================================================

document

    .querySelectorAll(

        ".move"

    )

    .forEach(

        button => {

            button.addEventListener(

                "pointerdown",

                async event => {

                    event.preventDefault();

                    if (

                        movingButton

                        &&

                        movingButton !== button

                    ) {

                        movingButton

                            .classList

                            .remove(

                                "active"

                            );

                    }

                    movingButton =

                        button;

                    button

                        .classList

                        .add(

                            "active"

                        );

                    setMotion(

                        button.dataset.name

                    );

                    try {

                        button.setPointerCapture(

                            event.pointerId

                        );

                    }

                    catch (_) {

                    }

                    await sendCommand(

                        button.dataset.command

                    );

                }

            );

            const finishMove =

                async event => {

                    event.preventDefault();

                    if (

                        movingButton !== button

                    ) {

                        return;

                    }

                    button

                        .classList

                        .remove(

                            "active"

                        );

                    movingButton = null;

                    setMotion(

                        "정지"

                    );

                    await sendCommand(

                        "stop_move"

                    );

                };

            button.addEventListener(

                "pointerup",

                finishMove

            );

            button.addEventListener(

                "pointercancel",

                finishMove

            );

        }

    );

// ========================================================

// STOP

// ========================================================

document

    .getElementById(

        "stopButton"

    )

    .addEventListener(

        "click",

        stopMovement

    );

// ========================================================

// LIGHT ON

// ========================================================

document

    .getElementById(

        "lightOn"

    )

    .addEventListener(

        "click",

        async () => {

            const ok =

                await sendCommand(

                    "light_on"

                );

            if (!ok) {

                return;

            }

            document

                .getElementById(

                    "lightText"

                )

                .textContent =

                    "현재: 양쪽 라이트 계속 ON";

            document

                .getElementById(

                    "lightOn"

                )

                .classList

                .add(

                    "light-selected"

                );

            document

                .getElementById(

                    "lightOff"

                )

                .classList

                .remove(

                    "light-selected"

                );

        }

    );

// ========================================================

// LIGHT OFF

// ========================================================

document

    .getElementById(

        "lightOff"

    )

    .addEventListener(

        "click",

        async () => {

            const ok =

                await sendCommand(

                    "light_off"

                );

            if (!ok) {

                return;

            }

            document

                .getElementById(

                    "lightText"

                )

                .textContent =

                    "현재: 자동 모드";

            document

                .getElementById(

                    "lightOn"

                )

                .classList

                .remove(

                    "light-selected"

                );

            document

                .getElementById(

                    "lightOff"

                )

                .classList

                .add(

                    "light-selected"

                );

        }

    );

// ========================================================

// 집게

// ========================================================

document

    .querySelectorAll(

        ".grip"

    )

    .forEach(

        button => {

            button.addEventListener(

                "pointerdown",

                async event => {

                    event.preventDefault();

                    button

                        .classList

                        .add(

                            "active"

                        );

                    try {

                        button.setPointerCapture(

                            event.pointerId

                        );

                    }

                    catch (_) {

                    }

                    await sendCommand(

                        button.dataset.command

                    );

                }

            );

            const stopGrip =

                async event => {

                    event.preventDefault();

                    button

                        .classList

                        .remove(

                            "active"

                        );

                    await sendCommand(

                        "grip_stop"

                    );

                };

            button.addEventListener(

                "pointerup",

                stopGrip

            );

            button.addEventListener(

                "pointercancel",

                stopGrip

            );

        }

    );

// ========================================================

// 화면에서 벗어나면 안전 정지

// ========================================================

window.addEventListener(

    "blur",

    () => {

        if (movingButton) {

            stopMovement();

        }

    }

);

document.addEventListener(

    "visibilitychange",

    () => {

        if (

            document.hidden

            &&

            movingButton

        ) {

            stopMovement();

        }

    }

);

// ========================================================

// 페이지가 닫힐 때

// ========================================================

window.addEventListener(

    "pagehide",

    () => {

        if (movingButton) {

            const data =

                JSON.stringify({

                    command:

                        "stop_move"

                });

            const blob =

                new Blob(

                    [data],

                    {

                        type:

                            "application/json"

                    }

                );

            navigator.sendBeacon(

                "/api/command",

                blob

            );

        }

    }

);

// ========================================================

// 현재 상태 불러오기

// ========================================================

async function loadStatus() {

    try {

        const response =

            await fetch(

                "/api/status",

                {

                    cache:

                        "no-store"

                }

            );

        if (!response.ok) {

            throw new Error();

        }

        const data =

            await response.json();

        setConnected(

            true

        );

        if (

            data.light_always_on

        ) {

            document

                .getElementById(

                    "lightText"

                )

                .textContent =

                    "현재: 양쪽 라이트 계속 ON";

            document

                .getElementById(

                    "lightOn"

                )

                .classList

                .add(

                    "light-selected"

                );

        }

        else {

            document

                .getElementById(

                    "lightText"

                )

                .textContent =

                    "현재: 자동 모드";

            document

                .getElementById(

                    "lightOff"

                )

                .classList

                .add(

                    "light-selected"

                );

        }

    }

    catch (error) {

        setConnected(

            false

        );

    }

}

// ========================================================

// 카메라 연결이 끊어지면 자동 재접속

// ========================================================

const cameraImage =

    document.getElementById(

        "robotCamera"

    );

cameraImage.onerror =

    function () {

        document

            .getElementById(

                "cameraState"

            )

            .textContent =

                "재연결 중";

        setTimeout(

            () => {

                cameraImage.src =

                    "/video_feed?t="

                    + Date.now();

            },

            1000

        );

    };

cameraImage.onload =

    function () {

        document

            .getElementById(

                "cameraState"

            )

            .textContent =

                "LIVE";

    };

// ========================================================
// 카메라 전체화면
// ========================================================

const appRoot =
    document.querySelector(
        ".app"
    );

const fullscreenButton =
    document.getElementById(
        "fullscreenButton"
    );

async function enterCameraFullscreen() {

    appRoot.classList.add(
        "camera-fullscreen"
    );

    document.body.style.overflow =
        "hidden";

    fullscreenButton.textContent =
        "✕";

    try {

        if (
            appRoot.requestFullscreen
            &&
            !document.fullscreenElement
        ) {

            await appRoot.requestFullscreen();

        }

    }

    catch (_) {
        // iPhone Safari 등에서 Fullscreen API가 제한되어도
        // CSS 전체화면은 그대로 유지한다.
    }

}

async function exitCameraFullscreen() {

    appRoot.classList.remove(
        "camera-fullscreen"
    );

    document.body.style.overflow =
        "";

    fullscreenButton.textContent =
        "⛶";

    try {

        if (document.fullscreenElement) {

            await document.exitFullscreen();

        }

    }

    catch (_) {
    }

}

fullscreenButton.addEventListener(
    "click",
    () => {

        if (
            appRoot.classList.contains(
                "camera-fullscreen"
            )
        ) {

            exitCameraFullscreen();

        }

        else {

            enterCameraFullscreen();

        }

    }
);

document.addEventListener(
    "fullscreenchange",
    () => {

        if (
            !document.fullscreenElement
            &&
            appRoot.classList.contains(
                "camera-fullscreen"
            )
        ) {

            appRoot.classList.remove(
                "camera-fullscreen"
            );

            document.body.style.overflow =
                "";

            fullscreenButton.textContent =
                "⛶";

        }

    }
);

loadStatus();

</script>

</body>

</html>

"""

# =========================================================

# HTTP Handler

# =========================================================

class MobileHandler(

    BaseHTTPRequestHandler

):

    # HTTP/1.1 사용

    protocol_version = "HTTP/1.1"

    # 브라우저 요청 로그가 계속 출력되는 것 방지

    def log_message(

        self,

        format,

        *args

    ):

        return

    # =====================================================

    # JSON 응답

    # =====================================================

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

    # =====================================================

    # GET

    # =====================================================

    def do_GET(

        self

    ):

        # =================================================

        # 모바일 GUI

        # =================================================

        if (

            self.path == "/"

            or

            self.path.startswith(

                "/?"

            )

        ):

            body = (

                MOBILE_HTML.encode(

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

        # =================================================

        # 현재 상태

        # =================================================

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

                        light_always_on

                }

            self.send_json(

                data

            )

            return

        # =================================================

        # 카메라 실시간 영상

        # =================================================

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

                    # -------------------------------------

                    # 새 프레임이 들어올 때까지 대기

                    # -------------------------------------

                    with camera_condition:

                        camera_condition.wait_for(

                            lambda:

                                (

                                    latest_frame_id

                                    != last_id

                                )

                                or

                                (

                                    not server_running

                                ),

                            timeout=2.0

                        )

                        if not server_running:

                            break

                        jpeg = (

                            latest_jpeg

                        )

                        frame_id = (

                            latest_frame_id

                        )

                    # -------------------------------------

                    # 아직 카메라 데이터가 없으면

                    # 다시 기다림

                    # -------------------------------------

                    if jpeg is None:

                        continue

                    # 같은 프레임이면 보내지 않음

                    if frame_id == last_id:

                        continue

                    last_id = frame_id

                    # -------------------------------------

                    # MJPEG 프레임 전송

                    # -------------------------------------

                    header = (

                        b"--frame\r\n"

                        b"Content-Type: image/jpeg\r\n"

                        +

                        (

                            f"Content-Length: "

                            f"{len(jpeg)}\r\n\r\n"

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

        # =================================================

        # 존재하지 않는 주소

        # =================================================

        self.send_error(

            404

        )

    # =====================================================

    # POST

    # =====================================================

    def do_POST(

        self

    ):

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

        "파일 : GUIServer_mobile.py"

    )

    print(

        f"WEB PORT     : {WEB_PORT}"

    )

    print(

        f"DRIVE SPEED  : {DRIVE_SPEED}"

    )

    print(

        f"TURN SPEED   : {TURN_SPEED}"

    )

    print(

        f"CAMERA       : "

        f"{CAMERA_WIDTH}x{CAMERA_HEIGHT} "

        f"{CAMERA_FPS}FPS"

    )

    print(

        f"CAMERA SERVO : "

        f"{CAMERA_FIXED_ANGLE}도 고정"

    )

    print(

        "LIGHT MODE   : 자동"

    )

    print(
        f"YOLO MODEL   : {YOLO_MODEL_PATH}"
    )

    print(

        "=========================================="

    )

    # =====================================================

    # OLED 눈동자 시작

    # =====================================================

    global oled_eyes

    oled_eyes = OLEDEyes(
        port=1,
        address=0x3C
    )

    if oled_eyes.connected:
        oled_eyes.start()
        oled_set_direction("stop")

    # =====================================================
    # YOLO 객체 탐지 시작
    # =====================================================

    try:
        yolo_detector = YOLODetector(
            model_path=YOLO_MODEL_PATH,
            confidence=YOLO_CONFIDENCE,
            imgsz=YOLO_IMAGE_SIZE,
            detect_every_n_frames=YOLO_DETECT_EVERY_N_FRAMES,
        )
    except Exception as e:
        yolo_detector = None
        print("[YOLO] 초기화 실패:", repr(e))
        print("[YOLO] 객체 탐지 없이 카메라만 실행합니다.")

    # =====================================================

    # 모터 초기화

    # =====================================================

    move.setup()

    # =====================================================

    # 라이트 초기화

    # =====================================================

    try:

        switch.switchSetup()

        switch.set_all_switch_off()

    except Exception as e:

        print(

            "[라이트 초기화 경고]",

            e

        )

    # =====================================================

    # Servo 초기화

    # =====================================================

    initialize_servos()

    # =====================================================

    # 시작할 때 안전 정지

    # =====================================================

    safe_stop(

        force_lights_off=True

    )

    # =====================================================

    # 카메라 Thread 시작

    # =====================================================

    threading.Thread(

        target=camera_worker,

        daemon=True,

        name="CameraThread"

    ).start()

    # =====================================================

    # HTTP Server

    # =====================================================

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

        f"http://<라즈베리파이-IP>:{WEB_PORT}"

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

        # 기다리고 있는 카메라 스트림 Thread 깨우기

        with camera_condition:

            camera_condition.notify_all()

        # 로봇 정지

        safe_stop(

            force_lights_off=True

        )

        # 집게 정지

        try:

            gripper_servo.stopWiggle()

        except Exception:
            pass

        # 웹 서버 종료

        try:

            server.server_close()

        except Exception:

            pass

        # 모터 종료

        try:

            move.destroy()

        except Exception:

            pass

        # 라이트 종료

        try:

            switch.set_all_switch_off()

            switch.switchClose()

        except Exception:

            pass

        # OLED 눈동자 종료

        try:

            if oled_eyes is not None:

                oled_eyes.set_direction("stop")

                time.sleep(0.1)

                oled_eyes.stop()

                if oled_eyes.is_alive():

                    oled_eyes.join(timeout=1.0)

        except Exception:

            pass

        print(

            "[서버] 종료 완료"

        )

# =========================================================

# 실행

# =========================================================

if __name__ == "__main__":

    main()
