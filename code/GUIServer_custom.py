#!/usr/bin/env python3

# -*- coding: utf-8 -*-



"""

PiCar-Pro custom GUI server



목표

- robot_gui.py와 TCP 10223으로 통신

- 전진 / 후진 / 전진 좌우회전 / 후진 좌우회전

- 조향 중앙값은 RPIservo.py의 init_pwm0 값을 사용 (현재 사용자 설정: 60)

- 카메라 목(Servo 1)은 소프트웨어에서 절대 움직이지 않음

- 카메라 영상은 Servo 제어가 들어있는 공식 FPV.py를 사용하지 않고 직접 전송

- 로봇 팔 / 집게 기본 명령 지원

"""



import base64

import json

import socket

import threading

import time



import cv2

import zmq

from picamera2 import Picamera2



import Move as move

import RPIservo

import Switch as switch

from OLED_eyes import OLEDEyes
from YOLO_detector import YOLODetector





# =========================================================

# 네트워크 설정

# =========================================================



HOST = ""

PORT = 10223

BUFSIZ = 1024

VIDEO_PORT = 5555





# =========================================================

# 주행 설정

# =========================================================



speed_set = 25

TURN_SPEED = 30

TURN_ANGLE = 30



# Adeept 공식 코드와 같은 방향 변수

Dv = -1

# 후레시 상태
# False: 이동 방향에 따라 점등
# True : 방향과 관계없이 양쪽 계속 ON
light_always_on = False
current_motion = "stop"

# OLED / YOLO
oled_eyes = None
yolo_detector = None

YOLO_MODEL_PATH = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35
YOLO_IMAGE_SIZE = 320
YOLO_DETECT_EVERY_N_FRAMES = 3


def oled_set_direction(direction):
    if oled_eyes is None:
        return
    try:
        oled_eyes.set_direction(direction)
    except Exception as e:
        print("[OLED] 방향 전달 오류:", e)





# =========================================================

# Servo 초기화

# =========================================================



# 조향 Servo (0번)만 초기화한다.

# RPIservo.py의 init_pwm0 = 60 이면 이 위치가 정면이다.

steering = RPIservo.ServoCtrl()

steering.moveServoInit([0])



# 중요:

# 카메라 목은 Servo 1번이지만, 현재는 고정 상태로 사용할 것이므로

# Servo 1번 객체를 만들거나 moveInit()/moveAngle()을 호출하지 않는다.

# 따라서 이 서버 코드에서는 카메라 목에 어떤 위치 명령도 보내지 않는다.



# 팔 / 손목 / 집게

arm_servo = RPIservo.ServoCtrl()      # Servo 2

arm_servo.start()



hand_servo = RPIservo.ServoCtrl()     # Servo 3

hand_servo.start()



gripper_servo = RPIservo.ServoCtrl()  # Servo 4

gripper_servo.start()





# =========================================================

# 안전 정지

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
    print("[후레시] ON - 양쪽 계속 켜짐")


def light_off_mode():

    global light_always_on
    light_always_on = False
    apply_motion_lights(current_motion)
    print("[후레시] OFF - 이동 방향 점등 모드")


def safe_stop(force_lights_off=False):

    """모터 정지 + 조향 중앙. 일반 정지에서는 후레시 ON 상태를 유지한다."""

    global current_motion

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



# =========================================================

# 이동 명령

# =========================================================



def robot_ctrl(command):

    """robot_gui.py에서 보내는 이동/팔 명령 처리."""

    global current_motion

    if command == "forward":
        current_motion = "forward"
        oled_set_direction(current_motion)
        move.move(speed_set, 1, "mid")
        apply_motion_lights(current_motion)

    elif command == "backward":
        current_motion = "backward"
        oled_set_direction(current_motion)
        move.move(speed_set, -1, "mid")
        apply_motion_lights(current_motion)

    elif command == "DS":
        current_motion = "stop"
        oled_set_direction(current_motion)
        move.motorStop()
        apply_motion_lights(current_motion)

    elif command == "left":
        current_motion = "left"
        oled_set_direction(current_motion)
        steering.moveAngle(0, TURN_ANGLE * Dv)
        time.sleep(0.05)
        move.move(TURN_SPEED, 1, "mid")
        apply_motion_lights(current_motion)

    elif command == "right":
        current_motion = "right"
        oled_set_direction(current_motion)
        steering.moveAngle(0, -TURN_ANGLE * Dv)
        time.sleep(0.05)
        move.move(TURN_SPEED, 1, "mid")
        apply_motion_lights(current_motion)

    elif command == "backleft":
        current_motion = "backleft"
        oled_set_direction(current_motion)
        steering.moveAngle(0, TURN_ANGLE * Dv)
        time.sleep(0.05)
        move.move(TURN_SPEED, -1, "mid")
        apply_motion_lights(current_motion)

    elif command == "backright":
        current_motion = "backright"
        oled_set_direction(current_motion)
        steering.moveAngle(0, -TURN_ANGLE * Dv)
        time.sleep(0.05)
        move.move(TURN_SPEED, -1, "mid")
        apply_motion_lights(current_motion)

    elif command == "TS":
        current_motion = "stop"
        oled_set_direction(current_motion)
        steering.moveAngle(0, 0)
        move.motorStop()
        apply_motion_lights(current_motion)

    elif command == "light_on":
        light_on_mode()

    elif command == "light_off":
        light_off_mode()

    # 카메라 목은 현재 완전 고정. Servo 1에는 아무 명령도 보내지 않는다.
    elif command in ("lookleft", "lookright", "LRstop"):
        pass

    elif command == "armup":
        arm_servo.singleServo(2, -1, 5)

    elif command == "armdown":
        arm_servo.singleServo(2, 1, 5)

    elif command == "armstop":
        arm_servo.stopWiggle()

    elif command == "handup":
        hand_servo.singleServo(3, 1, 5)

    elif command == "handdown":
        hand_servo.singleServo(3, -1, 5)

    elif command == "HAstop":
        hand_servo.stopWiggle()

    elif command == "grab":
        gripper_servo.singleServo(4, -1, 5)

    elif command == "loose":
        gripper_servo.singleServo(4, 1, 5)

    elif command == "stop":
        gripper_servo.stopWiggle()

    elif command == "home":
        arm_servo.moveServoInit([2])
        hand_servo.moveServoInit([3])
        gripper_servo.moveServoInit([4])



# =========================================================

# 카메라 영상 전송

# =========================================================



def camera_stream(client_ip, stop_event):

    """

    Picamera2 영상을 robot_gui.py의 5555 포트로 전송한다.



    공식 FPV.py를 import하지 않는 이유:

    FPV.py는 import 시 여러 Servo를 moveInit()하여 카메라 목까지

    움직일 수 있기 때문이다. 이 함수는 카메라 영상만 처리한다.

    """

    global yolo_detector

    context = None

    footage_socket = None

    camera = None



    try:

        context = zmq.Context()

        footage_socket = context.socket(zmq.PAIR)

        footage_socket.setsockopt(zmq.LINGER, 0)

        footage_socket.connect(f"tcp://{client_ip}:{VIDEO_PORT}")



        camera = Picamera2()

        config = camera.create_preview_configuration(

            main={

                "format": "RGB888",

                "size": (640, 480),

            }

        )

        camera.configure(config)

        camera.start()



        print(f"[카메라] {client_ip}:{VIDEO_PORT} 영상 전송 시작")



        while not stop_event.is_set():

            frame = camera.capture_array()



            if frame is None:

                time.sleep(0.02)

                continue



            # Picamera2 RGB -> OpenCV BGR

            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            # YOLO 객체 탐지 + 사각형 표시
            if yolo_detector is not None:
                frame_bgr = yolo_detector.detect_and_draw(frame_bgr)

            ok, encoded = cv2.imencode(

                ".jpg",

                frame_bgr,

                [int(cv2.IMWRITE_JPEG_QUALITY), 80],

            )



            if not ok:

                continue



            jpg_text = base64.b64encode(encoded.tobytes())

            footage_socket.send(jpg_text)



    except Exception as e:

        # 카메라가 없어도 모터 제어 서버는 계속 동작한다.

        print("[카메라] 사용하지 못함:", e)



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



        print("[카메라] 영상 전송 종료")





# =========================================================

# 클라이언트 처리

# =========================================================



def handle_client(client_socket, client_address):

    global speed_set



    client_ip = client_address[0]

    camera_stop_event = threading.Event()



    print("[클라이언트] 연결:", client_address)



    # GUI가 5555 수신 소켓을 먼저 열고 TCP로 연결하므로

    # 연결 직후 카메라 전송 Thread를 시작한다.

    camera_thread = threading.Thread(

        target=camera_stream,

        args=(client_ip, camera_stop_event),

        daemon=True,

    )

    camera_thread.start()



    try:

        while True:

            raw = client_socket.recv(BUFSIZ)



            if not raw:

                break



            command = raw.decode(errors="ignore").strip()



            if not command:

                continue



            print("[명령]", command)



            response = {

                "status": "ok",

                "title": "",

                "data": None,

            }



            # 속도 변경

            if command.startswith("wsB "):

                try:

                    value = int(command.split()[1])

                    speed_set = max(0, min(100, value))

                except Exception:

                    response["status"] = "error"

                    response["data"] = "잘못된 속도 값"



            else:

                try:

                    robot_ctrl(command)

                except Exception as e:

                    print("[명령 처리 오류]", e)

                    response["status"] = "error"

                    response["data"] = str(e)



            client_socket.sendall(

                json.dumps(response).encode()

            )



    except (ConnectionResetError, BrokenPipeError):

        pass



    except Exception as e:

        print("[클라이언트 오류]", e)



    finally:

        camera_stop_event.set()

        safe_stop(force_lights_off=True)



        try:

            client_socket.close()

        except Exception:

            pass



        print("[클라이언트] 연결 종료")





# =========================================================

# 서버 시작

# =========================================================



def main():

    global oled_eyes, yolo_detector

    print("==========================================")

    print(" PiCar-Pro Custom GUI Server")

    print("==========================================")

    print("조향 중앙값: RPIservo.py init_pwm0 사용")

    print("카메라 목: 고정 (Servo 1 제어 안 함)")

    print("지원: 전진/후진/좌우/후진좌우/집게/카메라 영상")

    print("==========================================")

    # OLED 눈동자 시작
    oled_eyes = OLEDEyes(port=1, address=0x3C)
    if oled_eyes.connected:
        oled_eyes.start()
        oled_set_direction("stop")

    # YOLO 객체 탐지 시작
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

    move.setup()



    try:

        switch.switchSetup()

        switch.set_all_switch_off()

    except Exception as e:

        print("[라이트 초기화 경고]", e)



    safe_stop(force_lights_off=True)



    server_socket = socket.socket(

        socket.AF_INET,

        socket.SOCK_STREAM,

    )

    server_socket.setsockopt(

        socket.SOL_SOCKET,

        socket.SO_REUSEADDR,

        1,

    )

    server_socket.bind((HOST, PORT))

    server_socket.listen(1)



    print(f"[서버] PORT {PORT}에서 연결 대기")



    try:

        while True:

            client_socket, client_address = server_socket.accept()

            handle_client(client_socket, client_address)



    except KeyboardInterrupt:

        print("\n[서버] 종료 요청")



    finally:

        safe_stop(force_lights_off=True)



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

                oled_eyes.join(timeout=1)

        except Exception:

            pass



        try:

            server_socket.close()

        except Exception:

            pass



        print("[서버] 종료 완료")





if __name__ == "__main__":

    main()