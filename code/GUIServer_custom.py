#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
파일 이름: GUIServer_custom.py
용도: PiCar-Pro PC GUI 서버

이번 수정
- PC 주행 응답 개선
- 전진/후진 속도 50, 회전 속도 40
- YOLO 부하 추가 감소: 224 / 8프레임마다 탐지
- 카메라 전송과 YOLO 추론 분리 유지
- TCP_NODELAY 적용
- 집게 지연 0.12 제거
- 집게 시작 전 stopWiggle + sleep 제거
"""

import os

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import base64
import json
import socket
import threading
import time

import cv2
import torch
import zmq

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
# 네트워크
# =========================================================

HOST = ""
PORT = 10223
BUFSIZ = 1024
VIDEO_PORT = 5555


# =========================================================
# 주행 설정
# =========================================================

speed_set = 50

TURN_SPEED = 40
TURN_ANGLE = 30

Dv = -1

control_lock = threading.RLock()


# =========================================================
# 카메라
# =========================================================

CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 15
JPEG_QUALITY = 65


# =========================================================
# YOLO
# =========================================================

YOLO_MODEL_PATH = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35

YOLO_IMAGE_SIZE = 224

YOLO_DETECT_EVERY_N_FRAMES = 8

yolo_detector = None
async_yolo = None


# =========================================================
# OLED / 상태
# =========================================================

oled_eyes = None

light_always_on = False

current_motion = "stop"


# =========================================================
# 집게
# =========================================================

GRIPPER_SPEED = 5

gripper_lock = threading.Lock()


# =========================================================
# Servo
# =========================================================

steering = RPIservo.ServoCtrl()

steering.moveServoInit([0])


arm_servo = RPIservo.ServoCtrl()

arm_servo.start()


hand_servo = RPIservo.ServoCtrl()

hand_servo.start()


gripper_servo = RPIservo.ServoCtrl()

gripper_servo.start()


# =========================================================
# 비동기 YOLO
# =========================================================

class AsyncYOLO:

    def __init__(
        self,
        detector
    ):

        self.detector = detector

        self.frame_lock = (
            threading.Lock()
        )

        self.result_lock = (
            threading.Lock()
        )

        self.event = (
            threading.Event()
        )

        self.stop_event = (
            threading.Event()
        )

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


    def submit(
        self,
        frame
    ):

        if self.detector is None:
            return

        with self.frame_lock:

            self.latest_frame = (
                frame.copy()
            )

        self.event.set()


    def draw(
        self,
        frame
    ):

        if self.detector is None:
            return frame

        with self.result_lock:

            detections = list(
                self.latest_detections
            )

        try:

            return (
                self.detector
                ._draw_detections(
                    frame,
                    detections
                )
            )

        except Exception as e:

            print(
                "[YOLO] 표시 오류:",
                repr(e)
            )

            return frame


    def _worker(self):

        print(
            "[YOLO] 비동기 탐지 Thread 시작"
        )

        while not self.stop_event.is_set():

            self.event.wait(
                timeout=0.2
            )

            if self.stop_event.is_set():
                break

            if not self.event.is_set():
                continue

            self.event.clear()


            with self.frame_lock:

                if self.latest_frame is None:
                    continue

                frame = (
                    self.latest_frame.copy()
                )


            try:

                detections = (
                    self.detector
                    ._run_inference(
                        frame
                    )
                )


                with self.result_lock:

                    self.latest_detections = (
                        detections
                    )


            except Exception as e:

                print(
                    "[YOLO] 탐지 오류:",
                    repr(e)
                )

                time.sleep(
                    0.05
                )


        print(
            "[YOLO] 비동기 탐지 Thread 종료"
        )


    def stop(self):

        self.stop_event.set()

        self.event.set()

        if (
            self.thread is not None
            and
            self.thread.is_alive()
        ):

            self.thread.join(
                timeout=1.0
            )


# =========================================================
# OLED
# =========================================================

def oled_set_direction(
    direction
):

    if oled_eyes is None:
        return

    try:

        oled_eyes.set_direction(
            direction
        )

    except Exception as e:

        print(
            "[OLED] 방향 전달 오류:",
            e
        )


# =========================================================
# 라이트
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


def apply_motion_lights(
    motion=None
):

    if motion is None:

        motion = current_motion


    if light_always_on:

        both_lights_on()

        return


    if motion in (
        "forward",
        "backward"
    ):

        both_lights_on()


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


    else:

        both_lights_off()


def light_on_mode():

    global light_always_on

    light_always_on = True

    both_lights_on()

    print(
        "[후레시] ON - 양쪽 계속 켜짐"
    )


def light_off_mode():

    global light_always_on

    light_always_on = False

    apply_motion_lights(
        current_motion
    )

    print(
        "[후레시] OFF - 이동 방향 점등 모드"
    )


# =========================================================
# 집게
# =========================================================

def gripper_start(
    direction
):

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
# 안전 정지
# =========================================================

def safe_stop(
    force_lights_off=False
):

    global current_motion


    with control_lock:

        try:

            move.motorStop()

        except Exception:

            pass


        try:

            steering.moveAngle(
                0,
                0
            )

        except Exception:

            pass


        current_motion = "stop"


        oled_set_direction(
            "stop"
        )


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
# 주행
# =========================================================

def drive_command(
    motion,
    direction,
    steering_offset,
    speed
):

    global current_motion


    with control_lock:

        current_motion = (
            motion
        )


        oled_set_direction(
            motion
        )


        steering.moveAngle(

            0,

            steering_offset

        )


        move.move(

            speed,

            direction,

            "mid"

        )


        apply_motion_lights(
            motion
        )


# =========================================================
# 명령 처리
# =========================================================

def robot_ctrl(
    command
):


    if command == "forward":

        drive_command(

            "forward",

            1,

            0,

            speed_set

        )


    elif command == "backward":

        drive_command(

            "backward",

            -1,

            0,

            speed_set

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


    elif command in (
        "DS",
        "TS"
    ):

        safe_stop(
            force_lights_off=False
        )


    elif command == "light_on":

        light_on_mode()


    elif command == "light_off":

        light_off_mode()


    elif command in (

        "lookleft",
        "lookright",
        "LRstop"

    ):

        pass


    elif command == "armup":

        arm_servo.singleServo(

            2,
            -1,
            5

        )


    elif command == "armdown":

        arm_servo.singleServo(

            2,
            1,
            5

        )


    elif command == "armstop":

        arm_servo.stopWiggle()


    elif command == "handup":

        hand_servo.singleServo(

            3,
            1,
            5

        )


    elif command == "handdown":

        hand_servo.singleServo(

            3,
            -1,
            5

        )


    elif command == "HAstop":

        hand_servo.stopWiggle()


    elif command == "grab":

        gripper_start(
            -1
        )


    elif command == "loose":

        gripper_start(
            1
        )


    elif command == "stop":

        gripper_stop()


    elif command == "home":

        arm_servo.moveServoInit(
            [2]
        )

        hand_servo.moveServoInit(
            [3]
        )

        gripper_servo.moveServoInit(
            [4]
        )


# =========================================================
# 카메라
# =========================================================

def camera_stream(
    client_ip,
    stop_event
):

    context = None

    footage_socket = None

    camera = None


    frame_count = 0


    frame_interval = (

        1.0
        /
        CAMERA_FPS

    )


    try:

        context = (
            zmq.Context()
        )


        footage_socket = (

            context.socket(
                zmq.PAIR
            )

        )


        footage_socket.setsockopt(

            zmq.LINGER,

            0

        )


        footage_socket.setsockopt(

            zmq.SNDHWM,

            1

        )


        footage_socket.connect(

            f"tcp://"
            f"{client_ip}:"
            f"{VIDEO_PORT}"

        )


        camera = Picamera2()


        config = (

            camera
            .create_preview_configuration(

                main={

                    "format":
                        "RGB888",

                    "size": (

                        CAMERA_WIDTH,

                        CAMERA_HEIGHT

                    )

                }

            )

        )


        camera.configure(
            config
        )


        camera.start()


        time.sleep(
            1.0
        )


        print(

            f"[카메라] "

            f"{client_ip}:"
            f"{VIDEO_PORT} "

            f"{CAMERA_WIDTH}x"
            f"{CAMERA_HEIGHT} "

            f"{CAMERA_FPS}FPS "

            f"영상 전송 시작"

        )


        while not stop_event.is_set():


            loop_start = (
                time.monotonic()
            )


            frame = (
                camera.capture_array()
            )


            if frame is None:

                time.sleep(
                    0.01
                )

                continue


            frame_bgr = (

                cv2.cvtColor(

                    frame,

                    cv2.COLOR_RGB2BGR

                )

            )


            frame_count += 1


            if (

                async_yolo
                is not None

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

                frame_bgr = (

                    async_yolo.draw(
                        frame_bgr
                    )

                )


            ok, encoded = (

                cv2.imencode(

                    ".jpg",

                    frame_bgr,

                    [

                        int(
                            cv2.IMWRITE_JPEG_QUALITY
                        ),

                        JPEG_QUALITY

                    ]

                )

            )


            if not ok:

                continue


            jpg_text = (

                base64.b64encode(

                    encoded.tobytes()

                )

            )


            try:

                footage_socket.send(

                    jpg_text,

                    flags=zmq.NOBLOCK

                )


            except zmq.Again:

                pass


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

        print(

            "[카메라] 사용하지 못함:",

            repr(e)

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


        if footage_socket is not None:

            try:

                footage_socket.close()

            except Exception:

                pass


        if context is not None:

            try:

                context.term()

            except Exception:

                pass


        print(
            "[카메라] 영상 전송 종료"
        )


# =========================================================
# PC 클라이언트
# =========================================================

def handle_client(
    client_socket,
    client_address
):

    global speed_set


    client_ip = (
        client_address[0]
    )


    try:

        client_socket.setsockopt(

            socket.IPPROTO_TCP,

            socket.TCP_NODELAY,

            1

        )

    except Exception:

        pass


    camera_stop_event = (
        threading.Event()
    )


    print(

        "[클라이언트] 연결:",

        client_address

    )


    camera_thread = (

        threading.Thread(

            target=camera_stream,

            args=(

                client_ip,

                camera_stop_event

            ),

            daemon=True,

            name="CameraStreamThread"

        )

    )


    camera_thread.start()


    try:


        while True:


            raw = (

                client_socket.recv(
                    BUFSIZ
                )

            )


            if not raw:

                break


            command = (

                raw.decode(
                    errors="ignore"
                )

                .strip()

            )


            if not command:

                continue


            print(
                "[명령]",
                command
            )


            response = {

                "status":
                    "ok",

                "title":
                    "",

                "data":
                    None

            }


            if command.startswith(
                "wsB "
            ):


                try:


                    value = int(

                        command
                        .split()[1]

                    )


                    speed_set = max(

                        0,

                        min(
                            100,
                            value
                        )

                    )


                except Exception:


                    response[
                        "status"
                    ] = "error"


                    response[
                        "data"
                    ] = "잘못된 속도 값"


            else:


                try:


                    robot_ctrl(
                        command
                    )


                except Exception as e:


                    print(

                        "[명령 처리 오류]",

                        repr(e)

                    )


                    response[
                        "status"
                    ] = "error"


                    response[
                        "data"
                    ] = str(e)


            client_socket.sendall(

                json.dumps(
                    response
                ).encode()

            )


    except (

        ConnectionResetError,

        BrokenPipeError

    ):

        pass


    except Exception as e:

        print(

            "[클라이언트 오류]",

            repr(e)

        )


    finally:


        camera_stop_event.set()


        gripper_stop()


        safe_stop(
            force_lights_off=True
        )


        try:

            client_socket.close()

        except Exception:

            pass


        print(
            "[클라이언트] 연결 종료"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    global oled_eyes

    global yolo_detector

    global async_yolo


    print(
        "=========================================="
    )

    print(
        " PiCar-Pro Custom GUI Server"
    )

    print(
        "=========================================="
    )

    print(

        f"DRIVE SPEED : "
        f"{speed_set}"

    )

    print(

        f"TURN SPEED  : "
        f"{TURN_SPEED}"

    )

    print(

        f"CAMERA      : "

        f"{CAMERA_WIDTH}x"
        f"{CAMERA_HEIGHT} "

        f"{CAMERA_FPS}FPS"

    )

    print(

        f"YOLO        : "

        f"{YOLO_MODEL_PATH}, "

        f"imgsz="
        f"{YOLO_IMAGE_SIZE}, "

        f"every="
        f"{YOLO_DETECT_EVERY_N_FRAMES}"

    )

    print(
        "GRIPPER     : 기본 delay 사용"
    )

    print(
        "=========================================="
    )


    oled_eyes = OLEDEyes(

        port=1,

        address=0x3C

    )


    if oled_eyes.connected:

        oled_eyes.start()

        oled_set_direction(
            "stop"
        )


    try:


        yolo_detector = (

            YOLODetector(

                model_path=
                    YOLO_MODEL_PATH,

                confidence=
                    YOLO_CONFIDENCE,

                imgsz=
                    YOLO_IMAGE_SIZE,

                detect_every_n_frames=
                    1

            )

        )


        async_yolo = (

            AsyncYOLO(
                yolo_detector
            )

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


    move.setup()


    try:

        switch.switchSetup()

        switch.set_all_switch_off()

    except Exception as e:

        print(
            "[라이트 초기화 경고]",
            e
        )


    safe_stop(
        force_lights_off=True
    )


    server_socket = (

        socket.socket(

            socket.AF_INET,

            socket.SOCK_STREAM

        )

    )


    server_socket.setsockopt(

        socket.SOL_SOCKET,

        socket.SO_REUSEADDR,

        1

    )


    server_socket.bind(

        (
            HOST,
            PORT
        )

    )


    server_socket.listen(
        1
    )


    print(

        f"[서버] PORT "

        f"{PORT}에서 연결 대기"

    )


    try:


        while True:


            client_socket, client_address = (

                server_socket.accept()

            )


            handle_client(

                client_socket,

                client_address

            )


    except KeyboardInterrupt:


        print(
            "\n[서버] 종료 요청"
        )


    finally:


        gripper_stop()


        safe_stop(
            force_lights_off=True
        )


        if async_yolo is not None:

            async_yolo.stop()


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

                oled_eyes.stop()

                if oled_eyes.is_alive():

                    oled_eyes.join(
                        timeout=1.0
                    )

        except Exception:

            pass


        try:

            server_socket.close()

        except Exception:

            pass


        print(
            "[서버] 종료 완료"
        )


if __name__ == "__main__":

    main()