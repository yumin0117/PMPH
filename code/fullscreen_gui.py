#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
파일 이름: fullscreen_gui.py
용도: PiCar-Pro PC 카메라 전체화면 GUI

이 파일은 서버나 로봇 하드웨어에 직접 연결하지 않는다.
robot_gui.py에서 전달받은 카메라 프레임과 기존 제어 함수를 사용한다.
"""

import tkinter as tk
from PIL import Image, ImageTk


BG = "#0E1116"
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


class FullscreenGUI:

    def __init__(
        self,
        master,
        get_frame,
        start_move,
        stop_move,
        arm_grab_start,
        arm_release_start,
        arm_stop,
        flash_on,
        flash_off,
        get_flash_state,
    ):
        self.master = master
        self.get_frame = get_frame
        self.start_move_callback = start_move
        self.stop_move_callback = stop_move
        self.arm_grab_start_callback = arm_grab_start
        self.arm_release_start_callback = arm_release_start
        self.arm_stop_callback = arm_stop
        self.flash_on_callback = flash_on
        self.flash_off_callback = flash_off
        self.get_flash_state = get_flash_state

        self.window = None
        self.camera_frame = None
        self.camera_label = None
        self.camera_job = None

        self.flash_on_button = None
        self.flash_off_button = None
        self.flash_status = None

    # =====================================================
    # 전체화면 열기
    # =====================================================

    def open(self):
        if self.window is not None:
            try:
                if self.window.winfo_exists():
                    self.window.lift()
                    self.window.focus_force()
                    return
            except Exception:
                pass

        self.window = tk.Toplevel(self.master)
        self.window.title("PiCar-Pro Robot Camera")
        self.window.configure(bg=BG)
        self.window.attributes("-fullscreen", True)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda event: self.close())
        self.window.bind("<F11>", lambda event: self.close())

        root = tk.Frame(self.window, bg=BG)
        root.pack(fill="both", expand=True, padx=18, pady=18)

        # =================================================
        # 왼쪽 카메라
        # =================================================

        camera_side = tk.Frame(root, bg=BG)
        camera_side.pack(side="left", fill="both", expand=True)

        camera_topbar = tk.Frame(camera_side, bg=BG)
        camera_topbar.pack(fill="x", pady=(0, 10))

        tk.Label(
            camera_topbar,
            text="로봇 카메라 · 전체화면",
            bg=BG,
            fg=WHITE,
            font=("TkDefaultFont", 13, "bold"),
        ).pack(side="left")

        tk.Label(
            camera_topbar,
            text="LIVE",
            bg=BG,
            fg=GREEN,
            font=("TkDefaultFont", 9),
        ).pack(side="right")

        self.camera_frame = tk.Frame(
            camera_side,
            bg=CAMERA_BG,
            highlightthickness=1,
            highlightbackground="#2B313A",
        )
        self.camera_frame.pack(fill="both", expand=True)
        self.camera_frame.pack_propagate(False)

        self.camera_label = tk.Label(
            self.camera_frame,
            text="로봇 카메라\n영상 연결 대기",
            bg=CAMERA_BG,
            fg=CAMERA_TEXT,
            font=("TkDefaultFont", 13, "bold"),
        )
        self.camera_label.pack(fill="both", expand=True)

        # =================================================
        # 오른쪽 제어 패널
        # =================================================

        panel = tk.Frame(root, bg=WHITE, width=310)
        panel.pack(side="right", fill="y", padx=(18, 0))
        panel.pack_propagate(False)

        tk.Label(
            panel,
            text="주행 제어",
            bg=WHITE,
            fg=TEXT,
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(18, 8))

        move_frame = tk.Frame(panel, bg=WHITE)
        move_frame.pack(padx=12)

        self._create_move_button(move_frame, "▲\n전진", "forward", 0, 1)
        self._create_move_button(move_frame, "◀\n좌회전", "left", 1, 0)

        stop_button = tk.Button(
            move_frame,
            text="■\nSTOP",
            width=9,
            height=2,
            bg=RED_LIGHT,
            fg=RED,
            activebackground=RED,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            font=("TkDefaultFont", 10, "bold"),
            command=self.stop_move_callback,
        )
        stop_button.grid(row=1, column=1, padx=5, pady=5)

        self._create_move_button(move_frame, "▶\n우회전", "right", 1, 2)
        self._create_move_button(move_frame, "↙\n후진 좌", "backleft", 2, 0)
        self._create_move_button(move_frame, "▼\n후진", "backward", 2, 1)
        self._create_move_button(move_frame, "↘\n후진 우", "backright", 2, 2)

        self._separator(panel)

        # =================================================
        # 후레시
        # =================================================

        tk.Label(
            panel,
            text="후레시",
            bg=WHITE,
            fg=TEXT,
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(0, 8))

        flash_row = tk.Frame(panel, bg=WHITE)
        flash_row.pack(fill="x", padx=18)

        self.flash_on_button = tk.Button(
            flash_row,
            text="ON",
            command=self._flash_on,
            bg=GREEN_LIGHT,
            fg=GREEN,
            activebackground=GREEN,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            width=10,
            height=2,
            font=("TkDefaultFont", 10, "bold"),
        )
        self.flash_on_button.pack(side="left", padx=(0, 5))

        self.flash_off_button = tk.Button(
            flash_row,
            text="OFF",
            command=self._flash_off,
            bg=BLUE_LIGHT,
            fg=BLUE,
            activebackground=BLUE,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            width=10,
            height=2,
            font=("TkDefaultFont", 10, "bold"),
        )
        self.flash_off_button.pack(side="left")

        self.flash_status = tk.Label(
            panel,
            text="OFF · 자동 방향등 모드",
            bg=WHITE,
            fg=SUBTEXT,
            font=("TkDefaultFont", 9),
        )
        self.flash_status.pack(anchor="w", padx=18, pady=(5, 0))

        self._set_flash_visual(bool(self.get_flash_state()))

        self._separator(panel)

        # =================================================
        # 집게
        # =================================================

        tk.Label(
            panel,
            text="로봇 팔",
            bg=WHITE,
            fg=TEXT,
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(0, 8))

        arm_row = tk.Frame(panel, bg=WHITE)
        arm_row.pack(fill="x", padx=18)

        grab_button = tk.Button(
            arm_row,
            text="잡기   Z",
            bg=GREEN_LIGHT,
            fg=GREEN,
            activebackground=GREEN,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            width=10,
            height=2,
            font=("TkDefaultFont", 10, "bold"),
        )
        grab_button.pack(side="left", padx=(0, 5))
        grab_button.bind(
            "<ButtonPress-1>",
            lambda event: self.arm_grab_start_callback(),
        )
        grab_button.bind(
            "<ButtonRelease-1>",
            lambda event: self.arm_stop_callback(),
        )

        release_button = tk.Button(
            arm_row,
            text="놓기   X",
            bg=BLUE_LIGHT,
            fg=BLUE,
            activebackground=BLUE,
            activeforeground=WHITE,
            relief="flat",
            bd=0,
            width=10,
            height=2,
            font=("TkDefaultFont", 10, "bold"),
        )
        release_button.pack(side="left")
        release_button.bind(
            "<ButtonPress-1>",
            lambda event: self.arm_release_start_callback(),
        )
        release_button.bind(
            "<ButtonRelease-1>",
            lambda event: self.arm_stop_callback(),
        )

        # 아래 여백
        tk.Frame(panel, bg=WHITE).pack(fill="both", expand=True)

        tk.Button(
            panel,
            text="⛶  전체화면 종료",
            command=self.close,
            bg="#EEF0F3",
            fg=TEXT,
            activebackground=BORDER,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            height=2,
            font=("TkDefaultFont", 10, "bold"),
        ).pack(fill="x", padx=18, pady=(10, 8))

        tk.Label(
            panel,
            text="ESC 또는 F11을 누르면 원래 화면으로 돌아갑니다.",
            bg=WHITE,
            fg=SUBTEXT,
            font=("TkDefaultFont", 9),
        ).pack(padx=18, pady=(0, 14))

        self._update_camera()
        self.window.focus_force()

    # =====================================================
    # 공통 UI
    # =====================================================

    def _separator(self, parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(
            fill="x",
            padx=18,
            pady=16,
        )

    def _create_move_button(self, parent, text, direction, row, column):
        button = tk.Button(
            parent,
            text=text,
            width=9,
            height=2,
            bg=WHITE,
            fg=TEXT,
            activebackground=BLUE_LIGHT,
            activeforeground=BLUE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            font=("TkDefaultFont", 10, "bold"),
        )
        button.grid(row=row, column=column, padx=5, pady=5)

        def press(event):
            button.config(bg=BLUE, fg=WHITE)
            self.start_move_callback(direction)

        def release(event):
            button.config(bg=WHITE, fg=TEXT)
            self.stop_move_callback()

        button.bind("<ButtonPress-1>", press)
        button.bind("<ButtonRelease-1>", release)
        button.bind("<Leave>", release)

    # =====================================================
    # 후레시
    # =====================================================

    def _flash_on(self):
        self.flash_on_callback()
        self._set_flash_visual(True)

    def _flash_off(self):
        self.flash_off_callback()
        self._set_flash_visual(False)

    def _set_flash_visual(self, enabled):
        if self.flash_on_button is None or self.flash_off_button is None:
            return

        if enabled:
            self.flash_on_button.config(bg=GREEN, fg=WHITE)
            self.flash_off_button.config(bg=BLUE_LIGHT, fg=BLUE)
            self.flash_status.config(text="ON · 양쪽 계속 켜짐", fg=GREEN)
        else:
            self.flash_on_button.config(bg=GREEN_LIGHT, fg=GREEN)
            self.flash_off_button.config(bg=BLUE, fg=WHITE)
            self.flash_status.config(text="OFF · 자동 방향등 모드", fg=BLUE)

    # =====================================================
    # 카메라 표시
    # =====================================================

    def _update_camera(self):
        if self.window is None:
            return

        try:
            if not self.window.winfo_exists():
                return
        except Exception:
            return

        frame = self.get_frame()

        if frame is not None:
            try:
                frame_height, frame_width = frame.shape[:2]
                area_width = self.camera_frame.winfo_width()
                area_height = self.camera_frame.winfo_height()

                if area_width > 10 and area_height > 10:
                    scale = min(
                        area_width / frame_width,
                        area_height / frame_height,
                    )

                    show_width = max(1, int(frame_width * scale))
                    show_height = max(1, int(frame_height * scale))

                    image = Image.fromarray(frame)
                    image = image.resize(
                        (show_width, show_height),
                        Image.Resampling.LANCZOS,
                    )
                    photo = ImageTk.PhotoImage(image)

                    self.camera_label.config(image=photo, text="")
                    self.camera_label.image = photo
            except Exception:
                pass

        try:
            self.camera_job = self.window.after(50, self._update_camera)
        except Exception:
            self.camera_job = None

    # =====================================================
    # 닫기
    # =====================================================

    def close(self):
        if self.window is not None and self.camera_job is not None:
            try:
                self.window.after_cancel(self.camera_job)
            except Exception:
                pass

        self.camera_job = None

        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

        self.window = None
        self.camera_frame = None
        self.camera_label = None
        self.flash_on_button = None
        self.flash_off_button = None
        self.flash_status = None
