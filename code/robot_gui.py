#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
파일 이름: robot_gui.py
용도: PiCar-Pro 컴퓨터용 Tkinter GUI

이번 수정
- 부드러운 속도 증가 기능 제거
- 전진/후진을 처음부터 50으로 실행
- 회전 속도 40
- 속도 변경 명령 반복 전송 제거
- 정지 시 DS/TS 두 번 보내던 것을 DS 한 번으로 변경
- TCP_NODELAY 적용
- 카메라 GUI LANCZOS -> BILINEAR
- 카메라 GUI 갱신 50ms -> 80ms
- 집게/이동 명령 Worker를 Event 방식으로 즉시 깨움
"""

import base64
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont

import cv2
import numpy as np
import zmq

from PIL import Image, ImageTk
from fullscreen_gui import FullscreenGUI

# =========================================================
# 서버 설정
# =========================================================

SERVER_IP = "127.0.0.1"
SERVER_PORT = 10223
VIDEO_PORT = 5555

# =========================================================
# 이동 속도 설정
# =========================================================

# 기존:
# START_SPEED = 20
# MAX_SPEED = 50
# 150ms마다 5씩 증가
#
# 변경:
# 누르는 즉시 고정 속도로 명령

DRIVE_SPEED = 50
TURN_SPEED = 40

CAMERA_GUI_UPDATE_MS = 80

# =========================================================
# 프로그램 상태
# =========================================================

app_running = True
server_connected = False

tcp_socket = None

socket_lock = threading.Lock()
state_lock = threading.Lock()

desired_direction = None
desired_speed = 0
current_speed = 0

last_sent_direction = None
last_sent_speed = None

pressed_keys = set()
release_jobs = {}

# 집게 / 후레시 명령
command_queue = queue.Queue()

# Worker를 즉시 깨우기 위한 Event
control_event = threading.Event()

flash_always_on = False

# =========================================================
# 카메라 상태
# =========================================================

zmq_context = None
footage_socket = None

latest_frame = None
latest_frame_number = 0
last_video_time = 0

frame_lock = threading.Lock()
displayed_frame_number = -1

# =========================================================
# 색상
# =========================================================

BG = "#F4F6F8"
WHITE = "#FFFFFF"
TEXT = "#191F28"
SUBTEXT = "#8B95A1"
BORDER = "#E5E8EB"

BLUE = "#3182F6"
BLUE_LIGHT = "#EAF2FF"

GREEN = "#20A06B"
GREEN_LIGHT = "#E8F7F0"

RED = "#E34D59"
RED_LIGHT = "#FDECEE"

CAMERA_BG = "#171A1F"
CAMERA_TEXT = "#AAB2BD"

# =========================================================
# GUI 생성
# =========================================================

window = tk.Tk()

window.title("PiCar-Pro Robot Control Center")

window.geometry("1180x840")

window.configure(bg=BG)

window.resizable(False, False)

# =========================================================
# 폰트
# =========================================================

default_font = tkfont.nametofont("TkDefaultFont")

title_font = default_font.copy()

title_font.configure(size=22, weight="bold")

camera_title_font = default_font.copy()

camera_title_font.configure(size=13, weight="bold")

section_font = default_font.copy()

section_font.configure(size=13, weight="bold")

button_font = default_font.copy()

button_font.configure(size=11, weight="bold")

value_font = default_font.copy()

value_font.configure(size=16, weight="bold")

small_font = default_font.copy()

small_font.configure(size=9)

# =========================================================
# 서버 상태
# =========================================================

def set_server_status(connected):
    if connected:
        server_dot.config(bg=GREEN)
        server_status.config(text="서버 연결됨", fg=GREEN)
    else:
        server_dot.config(bg=RED)
        server_status.config(text="서버 연결 안 됨", fg=RED)

# =========================================================
# 서버 명령 전송
# =========================================================

def send_and_wait(command):
    global server_connected
    if not server_connected:
        return False
    try:
        with socket_lock:
            tcp_socket.sendall(command.encode())
            tcp_socket.recv(4096)
        return True
    except Exception as e:
        print("[서버 통신 오류]", e)
        server_connected = False
        try:
            window.after(0, lambda: set_server_status(False))
        except Exception:
            pass
        return False

# =========================================================
# 서버 연결
# =========================================================

def connect_server():
    global tcp_socket
    global server_connected
    try:
        print(
            f"[서버] "
            f"{SERVER_IP}:"
            f"{SERVER_PORT} "
            f"연결 시도"
        )
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # 작은 이동 명령 지연 감소
        tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp_socket.settimeout(4)
        tcp_socket.connect((SERVER_IP, SERVER_PORT))
        tcp_socket.settimeout(2)
        server_connected = True
        print("[서버] 연결 완료")
        window.after(0, lambda: set_server_status(True))
        control_event.set()
    except Exception as e:
        server_connected = False
        print("[서버 연결 실패]", e)
        window.after(0, lambda: set_server_status(False))

# =========================================================
# 카메라 수신 준비
# =========================================================

def setup_video_receiver():
    global zmq_context
    global footage_socket
    try:
        zmq_context = zmq.Context()
        footage_socket = zmq_context.socket(zmq.PAIR)
        footage_socket.setsockopt(zmq.LINGER, 0)

        # 오래된 영상이 쌓이지 않게 제한
        footage_socket.setsockopt(zmq.RCVHWM, 1)
        footage_socket.setsockopt(zmq.RCVTIMEO, 1000)
        footage_socket.bind(
            f"tcp://*:{VIDEO_PORT}"
        )
        print(
            f"[카메라] "
            f"영상 수신 대기 "
            f"PORT {VIDEO_PORT}"
        )
    except Exception as e:
        print("[카메라 초기화 실패]", e)

# =========================================================
# 카메라 수신 Thread
# =========================================================

def video_receiver():
    global latest_frame
    global latest_frame_number
    global last_video_time
    while app_running:
        try:
            if footage_socket is None:
                time.sleep(0.2)
                continue
            frame_text = footage_socket.recv_string()
            image_bytes = base64.b64decode(frame_text)
            np_image = np.frombuffer(image_bytes, dtype=np.uint8)
            frame = cv2.imdecode(np_image, cv2.IMREAD_COLOR)
            if frame is None:
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            with frame_lock:
                latest_frame = frame
                latest_frame_number += 1
                last_video_time = time.time()
        except zmq.Again:
            continue
        except Exception as e:
            if app_running:
                print("[영상 수신 오류]", e)
            time.sleep(0.2)

# =========================================================
# 카메라 GUI 갱신
# =========================================================

def update_camera_gui():
    global displayed_frame_number
    if not app_running:
        return
    frame = None
    frame_number = -1
    last_time = 0
    with frame_lock:
        if latest_frame is not None:
            frame = latest_frame.copy()
            frame_number = latest_frame_number
            last_time = last_video_time
    if (
        frame is not None
        and
        frame_number
        !=
        displayed_frame_number
    ):
        displayed_frame_number = frame_number
        image = Image.fromarray(frame)

        # LANCZOS보다 훨씬 가벼운 BILINEAR 사용
        image = image.resize((520, 292), Image.Resampling.BILINEAR)
        photo = ImageTk.PhotoImage(image)
        robot_camera_label.config(image=photo, text="")
        robot_camera_label.image = photo
        robot_cam_status.config(text="LIVE", fg=GREEN)
    if (
        last_time == 0
        or
        time.time()
        -
        last_time
        >
        3
    ):
        robot_cam_status.config(text="영상 대기", fg=SUBTEXT)
    window.after(CAMERA_GUI_UPDATE_MS, update_camera_gui)

def get_latest_camera_frame():
    with frame_lock:
        if latest_frame is None:
            return None
        return latest_frame.copy()

# =========================================================
# 이동 이름
# =========================================================

def direction_name(direction):
    names = {
        "forward":
            "전진",
        "backward":
            "후진",
        "left":
            "좌회전",
        "right":
            "우회전",
        "backleft":
            "후진 좌회전",
        "backright":
            "후진 우회전",
    }
    return names.get(
        direction,
        "정지"
    )

# =========================================================
# 이동 시작
# =========================================================

def start_move(direction):
    global desired_direction
    global desired_speed
    global current_speed
    with state_lock:
        if (
            desired_direction
            ==
            direction
        ):
            return
        desired_direction = direction
        if direction in (
            "forward",
            "backward"
        ):
            desired_speed = DRIVE_SPEED
        else:
            desired_speed = TURN_SPEED
    if direction in (
        "forward",
        "backward"
    ):
        current_speed = DRIVE_SPEED
    else:
        current_speed = TURN_SPEED
    name = direction_name(direction)
    direction_value.config(text=name, fg=BLUE)
    speed_value.config(
        text=f"{current_speed}%"
    )
    speed_bar["value"] = (current_speed)
    motion_dot.config(bg=GREEN)
    motion_status.config(
        text=f"{name} 중"
    )
    print("[이동 시작]", name)

    # Polling 20ms를 기다리지 않고 즉시 Worker 실행
    control_event.set()

# =========================================================
# 이동 정지
# =========================================================

def stop_move():
    global desired_direction
    global desired_speed
    global current_speed
    with state_lock:
        desired_direction = None
        desired_speed = 0
    current_speed = 0
    direction_value.config(text="정지", fg=TEXT)
    speed_value.config(text="0%")
    speed_bar["value"] = 0
    motion_dot.config(bg=RED)
    motion_status.config(text="정지")
    print("[이동] 정지")
    control_event.set()

# =========================================================
# 집게
# =========================================================

def arm_grab_start():
    command_queue.put("grab")
    arm_value.config(text="잡는 중", fg=GREEN)
    print("[로봇팔] 잡기 시작")
    control_event.set()

def arm_release_start():
    command_queue.put("loose")
    arm_value.config(text="놓는 중", fg=BLUE)
    print("[로봇팔] 놓기 시작")
    control_event.set()

def arm_stop():
    command_queue.put("stop")
    if (
        arm_value.cget("text")
        ==
        "잡는 중"
    ):
        arm_value.config(text="잡기 유지", fg=GREEN)
    elif (
        arm_value.cget("text")
        ==
        "놓는 중"
    ):
        arm_value.config(text="놓기 유지", fg=BLUE)
    print("[로봇팔] 정지")
    control_event.set()

# =========================================================
# 후레시
# =========================================================

def flash_on():
    global flash_always_on
    flash_always_on = True
    command_queue.put("light_on")
    flash_value.config(text="ON", fg=GREEN)
    flash_on_button.config(bg=GREEN, fg=WHITE)
    flash_off_button.config(bg=WHITE, fg=TEXT)
    print("[후레시] ON - " "양쪽 라이트 계속 켜짐")
    control_event.set()

def flash_off():
    global flash_always_on
    flash_always_on = False
    command_queue.put("light_off")
    flash_value.config(text="AUTO", fg=BLUE)
    flash_on_button.config(bg=WHITE, fg=TEXT)
    flash_off_button.config(bg=BLUE, fg=WHITE)
    print("[후레시] OFF - " "자동 라이트 모드")
    control_event.set()

def get_flash_state():
    return (
        flash_always_on
    )

# =========================================================
# 서버 명령 Worker
# =========================================================

def command_worker():
    global last_sent_direction
    global last_sent_speed
    while app_running:
        if not server_connected:
            control_event.wait(timeout=0.1)
            control_event.clear()
            continue

        # -------------------------------------------------
        # 1. 이동 상태를 먼저 확인
        # -------------------------------------------------

        with state_lock:
            direction = desired_direction
            speed = desired_speed

        # 정지
        if direction is None:
            if (
                last_sent_direction
                is not None
            ):
                # 기존 DS + TS 두 번 전송 제거
                # 서버 DS 하나가 모터정지 + 조향중앙을 모두 처리
                send_and_wait("DS")
                last_sent_direction = None
                last_sent_speed = None

        # 전진 / 후진
        elif direction in (
            "forward",
            "backward"
        ):
            need_direction = last_sent_direction != direction
            need_speed = last_sent_speed != speed
            if need_speed:
                send_and_wait(
                    f"wsB {speed}"
                )
                last_sent_speed = speed
            if (
                need_direction
                or
                need_speed
            ):
                send_and_wait(direction)
                last_sent_direction = direction

        # 좌우 / 후진 좌우
        else:
            if (
                last_sent_direction
                !=
                direction
            ):
                send_and_wait(direction)
                last_sent_direction = direction
                last_sent_speed = TURN_SPEED

        # -------------------------------------------------
        # 2. 집게 / 라이트 명령
        # 대기 중인 명령은 바로 연속 처리
        # -------------------------------------------------

        while True:
            try:
                command = command_queue.get_nowait()
                send_and_wait(command)
            except queue.Empty:
                break

        # 새 명령이 들어오면 즉시 깨고,
        # Event를 놓쳐도 20ms 안에 다시 확인.
        control_event.wait(timeout=0.02)
        control_event.clear()

# =========================================================
# 키보드 방향 조합
# =========================================================

def resolve_keyboard_direction():
    up = "up" in pressed_keys
    down = "down" in pressed_keys
    left = "left" in pressed_keys
    right = "right" in pressed_keys
    if down and left:
        return "backleft"
    if down and right:
        return "backright"
    if up and left:
        return "left"
    if up and right:
        return "right"
    if up:
        return "forward"
    if down:
        return "backward"
    if left:
        return "left"
    if right:
        return "right"
    return None

def update_keyboard_movement():
    new_direction = resolve_keyboard_direction()
    with state_lock:
        active_direction = desired_direction
    if new_direction is None:
        if (
            active_direction
            is not None
        ):
            stop_move()
    elif (
        new_direction
        !=
        active_direction
    ):
        start_move(new_direction)

def key_pressed(event):
    key = event.keysym.lower()
    if key in release_jobs:
        try:
            window.after_cancel(release_jobs[key])
        except Exception:
            pass
        del release_jobs[key]
    if key in pressed_keys:
        return
    pressed_keys.add(key)
    if key in (
        "up",
        "down",
        "left",
        "right"
    ):
        update_keyboard_movement()
    elif key == "z":
        arm_grab_start()
    elif key == "x":
        arm_release_start()

def process_key_release(key):
    if key in release_jobs:
        del release_jobs[key]
    pressed_keys.discard(key)
    if key in (
        "up",
        "down",
        "left",
        "right"
    ):
        update_keyboard_movement()
    elif key in (
        "z",
        "x"
    ):
        arm_stop()

def key_released(event):
    key = event.keysym.lower()

    # 키 반복 때문에 순간적으로 Release가 발생하는 것 방지
    release_jobs[key] = window.after(20, lambda k=key: process_key_release(k))

# =========================================================
# 이동 버튼 생성
# =========================================================

def create_move_button(parent, text, direction, row, column):
    button = tk.Button(
        parent,
        text=text,
        width=9,
        height=2,
        bg=WHITE,
        fg=TEXT,
        activebackground=
            BLUE_LIGHT,
        activeforeground=
            BLUE,
        relief="flat",
        bd=0,
        highlightthickness=1,
        highlightbackground=
            BORDER,
        font=button_font,
    )
    button.grid(row=row, column=column, padx=5, pady=5)
    def press(
        event
    ):
        button.config(bg=BLUE, fg=WHITE)
        start_move(direction)
    def release(
        event
    ):
        button.config(bg=WHITE, fg=TEXT)
        stop_move()
    button.bind("<ButtonPress-1>", press)
    button.bind("<ButtonRelease-1>", release)
    button.bind("<Leave>", release)
    return button

# =========================================================
# 전체화면 GUI
# =========================================================

fullscreen_gui = FullscreenGUI(
    master=window,
    get_frame=
        get_latest_camera_frame,
    start_move=
        start_move,
    stop_move=
        stop_move,
    arm_grab_start=
        arm_grab_start,
    arm_release_start=
        arm_release_start,
    arm_stop=
        arm_stop,
    flash_on=
        flash_on,
    flash_off=
        flash_off,
    get_flash_state=
        get_flash_state,
)

# =========================================================
# 메인 Layout
# =========================================================

main = tk.Frame(window, bg=BG)

main.pack(fill="both", expand=True, padx=32, pady=20)

# =========================================================
# Header
# =========================================================

header = tk.Frame(main, bg=BG)

header.pack(fill="x", pady= (0, 14))

header_left = tk.Frame(header, bg=BG)

header_left.pack(side="left")

tk.Label(header_left, text="Robot Control Center", bg=BG, fg=TEXT, font=title_font,).pack(anchor="w")

tk.Label(header_left, text="PiCar-Pro · Raspberry Pi", bg=BG, fg=SUBTEXT,).pack(anchor="w", pady= (3, 0))

# =========================================================
# 서버 상태
# =========================================================

server_box = tk.Frame(
    header,
    bg=WHITE,
    highlightthickness=1,
    highlightbackground=BORDER,
    padx=13,
    pady=8,
)

server_box.pack(side="right")

server_dot = tk.Label(server_box, text="", bg=RED, width=1, height=1)

server_dot.pack(side="left", padx= (0, 8))

server_status = tk.Label(server_box, text="서버 연결 중", bg=WHITE, fg=SUBTEXT, font=small_font,)

server_status.pack(side="left")

# =========================================================
# 카메라 영역
# =========================================================

camera_row = tk.Frame(main, bg=BG)

camera_row.pack(fill="x")

# 로봇 카메라
robot_camera_card = tk.Frame(camera_row, bg=WHITE, highlightthickness=1, highlightbackground=BORDER,)

robot_camera_card.pack(side="left", padx= (0, 8), fill="both", expand=True)

robot_cam_header = tk.Frame(robot_camera_card, bg=WHITE)

robot_cam_header.pack(fill="x", padx=16, pady=11)

tk.Label(robot_cam_header, text="로봇 카메라", bg=WHITE, fg=TEXT, font=camera_title_font,).pack(side="left")

robot_cam_status = tk.Label(robot_cam_header, text="영상 대기", bg=WHITE, fg=SUBTEXT, font=small_font,)

fullscreen_button = tk.Button(
    robot_cam_header,
    text="⛶",
    command=fullscreen_gui.open,
    width=3,
    bg=WHITE,
    fg=TEXT,
    activebackground=
        BLUE_LIGHT,
    activeforeground=
        BLUE,
    relief="flat",
    bd=0,
    font=button_font,
)

fullscreen_button.pack(side="right")

robot_cam_status.pack(side="right", padx= (0, 8))

robot_video_frame = tk.Frame(robot_camera_card, bg=CAMERA_BG, width=520, height=292,)

robot_video_frame.pack(padx=12, pady= (0, 12))

robot_video_frame.pack_propagate(False)

robot_camera_label = tk.Label(
    robot_video_frame,
    text="로봇 카메라\n영상 연결 대기",
    bg=CAMERA_BG,
    fg=CAMERA_TEXT,
    font=section_font,
)

robot_camera_label.pack(fill="both", expand=True)

# 환경 카메라
environment_camera_card = tk.Frame(
    camera_row,
    bg=WHITE,
    highlightthickness=1,
    highlightbackground=BORDER,
)

environment_camera_card.pack(side="left", padx= (8, 0), fill="both", expand=True)

environment_header = tk.Frame(environment_camera_card, bg=WHITE)

environment_header.pack(fill="x", padx=16, pady=11)

tk.Label(environment_header, text="환경 카메라", bg=WHITE, fg=TEXT, font=camera_title_font,).pack(side="left")

tk.Label(environment_header, text="연결 대기", bg=WHITE, fg=SUBTEXT, font=small_font,).pack(side="right")

environment_video_frame = tk.Frame(environment_camera_card, bg=CAMERA_BG, width=520, height=292,)

environment_video_frame.pack(padx=12, pady= (0, 12))

environment_video_frame.pack_propagate(False)

environment_camera_label = tk.Label(
    environment_video_frame,
    text="환경 카메라\n연결 대기",
    bg=CAMERA_BG,
    fg=CAMERA_TEXT,
    font=section_font,
)

environment_camera_label.pack(fill="both", expand=True)

# =========================================================
# 하단 제어 카드
# =========================================================

control_card = tk.Frame(main, bg=WHITE, highlightthickness=1, highlightbackground=BORDER,)

control_card.pack(fill="x", pady= (14, 0))

# =========================================================
# 이동 제어
# =========================================================

move_section = tk.Frame(control_card, bg=WHITE)

move_section.pack(side="left", padx= (22, 12), pady=16)

tk.Label(
    move_section,
    text="이동 제어",
    bg=WHITE,
    fg=TEXT,
    font=section_font,
).pack(
    anchor="w",
    pady=(0, 7)
)

move_frame = tk.Frame(move_section, bg=WHITE)

move_frame.pack()

forward_button = create_move_button(move_frame, "▲\n전진", "forward", 0, 1)

left_button = create_move_button(move_frame, "◀\n좌회전", "left", 1, 0)

stop_display = tk.Label(
    move_frame,
    text="STOP",
    width=9,
    height=2,
    bg=RED_LIGHT,
    fg=RED,
    font=button_font,
)

stop_display.grid(row=1, column=1, padx=5, pady=5)

right_button = create_move_button(move_frame, "▶\n우회전", "right", 1, 2)

backleft_button = create_move_button(move_frame, "↙\n후진 좌", "backleft", 2, 0)

backward_button = create_move_button(move_frame, "▼\n후진", "backward", 2, 1)

backright_button = create_move_button(move_frame, "↘\n후진 우", "backright", 2, 2)

# =========================================================
# 현재 상태
# =========================================================

status_section = tk.Frame(control_card, bg=WHITE)

status_section.pack(side="left", padx=18, pady=20)

tk.Label(
    status_section,
    text="로봇 상태",
    bg=WHITE,
    fg=TEXT,
    font=section_font,
).pack(
    anchor="w",
    pady=(0, 12)
)

motion_status_frame = tk.Frame(status_section, bg=WHITE)

motion_status_frame.pack(anchor="w", pady= (0, 10))

motion_dot = tk.Label(motion_status_frame, bg=RED, width=1, height=1)

motion_dot.pack(side="left", padx= (0, 8))

motion_status = tk.Label(motion_status_frame, text="정지", bg=WHITE, fg=TEXT)

motion_status.pack(side="left")

info_row = tk.Frame(status_section, bg=WHITE)

info_row.pack()

# 방향
direction_box = tk.Frame(info_row, bg=WHITE)

direction_box.pack(side="left", padx= (0, 24))

tk.Label(direction_box, text="현재 방향", bg=WHITE, fg=SUBTEXT, font=small_font,).pack()

direction_value = tk.Label(direction_box, text="정지", bg=WHITE, fg=TEXT, font=value_font,)

direction_value.pack(pady= (4, 0))

# 속도
speed_box = tk.Frame(info_row, bg=WHITE)

speed_box.pack(side="left")

tk.Label(speed_box, text="현재 속도", bg=WHITE, fg=SUBTEXT, font=small_font,).pack()

speed_value = tk.Label(speed_box, text="0%", bg=WHITE, fg=BLUE, font=value_font,)

speed_value.pack(pady= (4, 0))

style = ttk.Style()

style.theme_use("clam")

style.configure(
    "Robot.Horizontal.TProgressbar",
    troughcolor="#EEF0F3",
    background=BLUE,
    bordercolor="#EEF0F3",
    lightcolor=BLUE,
    darkcolor=BLUE,
    thickness=7,
)

speed_bar = ttk.Progressbar(
    status_section,
    maximum=100,
    length=220,
    mode="determinate",
    style="Robot.Horizontal.TProgressbar",
)

speed_bar.pack(pady= (14, 0))

# =========================================================
# 후레시
# =========================================================

flash_section = tk.Frame(control_card, bg=WHITE)

flash_section.pack(side="left", padx=14, pady=18)

tk.Label(flash_section, text="후레시", bg=WHITE, fg=TEXT, font=section_font,).pack(pady= (0, 6))

flash_value = tk.Label(flash_section, text="AUTO", bg=WHITE, fg=BLUE, font=value_font,)

flash_value.pack(pady= (0, 8))

flash_on_button = tk.Button(
    flash_section,
    text="후레시 ON",
    width=13,
    height=2,
    bg=WHITE,
    fg=TEXT,
    activebackground=
        GREEN_LIGHT,
    activeforeground=
        GREEN,
    relief="flat",
    bd=0,
    highlightthickness=1,
    highlightbackground=BORDER,
    font=button_font,
    command=flash_on,
)

flash_on_button.pack(pady=3)

flash_off_button = tk.Button(
    flash_section,
    text="후레시 OFF",
    width=13,
    height=2,
    bg=BLUE,
    fg=WHITE,
    activebackground=
        BLUE_LIGHT,
    activeforeground=
        BLUE,
    relief="flat",
    bd=0,
    font=button_font,
    command=flash_off,
)

flash_off_button.pack(pady=3)

flash_help = tk.Label(flash_section, text="OFF = 자동 방향등", bg=WHITE, fg=SUBTEXT, font=small_font,)

flash_help.pack(pady= (4, 0))

# =========================================================
# 로봇 팔 / 집게
# =========================================================

arm_section = tk.Frame(control_card, bg=WHITE)

arm_section.pack(side="right", padx= (12, 22), pady=18)

tk.Label(arm_section, text="로봇 팔", bg=WHITE, fg=TEXT, font=section_font,).pack(pady= (0, 7))

arm_value = tk.Label(arm_section, text="대기", bg=WHITE, fg=SUBTEXT, font=value_font,)

arm_value.pack(pady= (0, 10))

grab_button = tk.Button(
    arm_section,
    text="잡기   Z",
    width=14,
    height=2,
    bg=GREEN_LIGHT,
    fg=GREEN,
    activebackground=
        GREEN,
    activeforeground=
        WHITE,
    relief="flat",
    bd=0,
    font=button_font,
)

grab_button.pack(pady=3)

grab_button.bind("<ButtonPress-1>", lambda event: arm_grab_start())

grab_button.bind("<ButtonRelease-1>", lambda event: arm_stop())

grab_button.bind("<Leave>", lambda event: arm_stop())

release_button = tk.Button(
    arm_section,
    text="놓기   X",
    width=14,
    height=2,
    bg=BLUE_LIGHT,
    fg=BLUE,
    activebackground=
        BLUE,
    activeforeground=
        WHITE,
    relief="flat",
    bd=0,
    font=button_font,
)

release_button.pack(pady=3)

release_button.bind("<ButtonPress-1>", lambda event: arm_release_start())

release_button.bind("<ButtonRelease-1>", lambda event: arm_stop())

release_button.bind("<Leave>", lambda event: arm_stop())

# =========================================================
# 프로그램 안전 종료
# =========================================================

def close_program():
    global app_running
    global server_connected
    fullscreen_gui.close()
    print("[시스템] 프로그램 종료")
    if server_connected:
        try:
            # DS 하나만으로 정지 + 조향중앙
            send_and_wait("DS")
            send_and_wait("stop")
            send_and_wait("light_off")
        except Exception:
            pass
    app_running = False
    server_connected = False
    control_event.set()
    try:
        if tcp_socket is not None:
            tcp_socket.close()
    except Exception:
        pass
    try:
        if footage_socket is not None:
            footage_socket.close()
    except Exception:
        pass
    try:
        if zmq_context is not None:
            zmq_context.term()
    except Exception:
        pass
    window.destroy()

# =========================================================
# 키보드 이벤트
# =========================================================

window.bind("<KeyPress>", key_pressed)

window.bind("<KeyRelease>", key_released)

window.bind("<F11>", lambda event: fullscreen_gui.open())

window.bind("<Escape>", lambda event: close_program())

window.protocol("WM_DELETE_WINDOW", close_program)

window.focus_set()

# =========================================================
# 시작 안내
# =========================================================

print("==========================================")

print(" PiCar-Pro Robot Control Center")

print("==========================================")

print(
    f"전진/후진 속도 : {DRIVE_SPEED}"
)

print(
    f"좌우 회전 속도 : {TURN_SPEED}"
)

print("↑ : 전진")

print("↓ : 후진")

print("← : 좌회전")

print("→ : 우회전")

print("↓ + ← : 후진 좌회전")

print("↓ + → : 후진 우회전")

print("Z : 로봇 팔 잡기")

print("X : 로봇 팔 놓기")

print("후레시 ON  : " "양쪽 라이트 계속 ON")

print("후레시 OFF : " "자동 방향등 모드")

print("ESC : 안전 종료")

print("==========================================")

# =========================================================
# 카메라 수신 Socket 먼저 준비
# =========================================================

setup_video_receiver()

# =========================================================
# Thread 시작
# =========================================================

video_thread = threading.Thread(target=video_receiver, daemon=True)

video_thread.start()

server_thread = threading.Thread(target=connect_server, daemon=True)

server_thread.start()

control_thread = threading.Thread(target=command_worker, daemon=True)

control_thread.start()

# =========================================================
# 카메라 GUI 갱신
# =========================================================

window.after(100, update_camera_gui)

# =========================================================
# GUI 시작
# =========================================================

try:
    window.mainloop()

except KeyboardInterrupt:
    close_program()
