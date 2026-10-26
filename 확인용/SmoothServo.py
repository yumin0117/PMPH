#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PiCar-Pro independent neck & gripper smooth controller.

Uses the installed RPIservo.py, not a second PCA9685 driver. No camera,
YOLO, Move.py, or extra pip dependency is loaded here.

The neutral offsets used by moveAngle are relative to RPIservo.init_pwmN.
Actual joints have no position feedback. Confirm orientation and clearance
before changing the conservative limits below.
"""

import threading
import time

# Confirmed by the user's NeckTest.py; verify on the physical robot.
NECK_CHANNELS = {"a_pan": 1, "a_tilt": 3, "b_tilt": 2}
NECK_SIGNS = {"a_pan": 1, "a_tilt": 1, "b_tilt": 1}
NECK_MAX_OFFSET = {"a_pan": 12, "a_tilt": 12, "b_tilt": 12}
NECK_STEP = 1                 # degrees per hardware update
NECK_TICK = 0.055            # nominal update interval (55 ms)
NECK_COMMAND_TIMEOUT = 0.8   # automatic hold release if client disconnects
GRIPPER_COMMAND_TIMEOUT = 0.9


class SmoothNeck:
    """Smoothly step axes while receiving heartbeats from held buttons.

    Command syntax: neck:a_pan:-1, neck:a_pan:0, neck:a_pan:1, etc.
    One servo I2C writer thread owns all neck axes. No abrupt recentering.
    """

    def __init__(self, servo_ctrl=None):
        if servo_ctrl is None:
            import RPIservo
            servo_ctrl = RPIservo.ServoCtrl()
        self.servo = servo_ctrl
        self.axes = tuple(NECK_CHANNELS)
        self.position = {axis: 0 for axis in self.axes}
        self.direction = {axis: 0 for axis in self.axes}
        self.deadline = {axis: 0.0 for axis in self.axes}
        self.last_seq = {axis: -1 for axis in self.axes}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.last_error = None

    def start(self):
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self._run, name="SmoothNeckWorker", daemon=True
        )
        self.worker.start()
        print("[목 제어] 서보 1(A 좌우), 3(A 상하), 2(B 상하) 준비")

    def command(self, message, seq=None):
        parts = message.split(":")
        if len(parts) != 3 or parts[0] != "neck" or parts[1] not in self.axes:
            raise ValueError("목 관절 명령 오류: " + message)
        try:
            direction = int(parts[2])
        except (TypeError, ValueError):
            raise ValueError("목 방향 명령은 -1, 0, 1만 가능합니다") from None
        if direction not in (-1, 0, 1):
            raise ValueError("목 방향 명령은 -1, 0, 1만 가능합니다")
        axis = parts[1]
        with self.lock:
            if seq is not None:
                if isinstance(seq, bool):
                    raise ValueError("잘못된 seq")
                seq = int(seq)
                if seq <= self.last_seq[axis]:
                    return
                self.last_seq[axis] = seq
            self.direction[axis] = direction
            self.deadline[axis] = time.monotonic() + NECK_COMMAND_TIMEOUT if direction else 0.0

    def release_all(self):
        with self.lock:
            for axis in self.axes:
                self.direction[axis] = 0
                self.deadline[axis] = 0.0

    def _run(self):
        # An initial position is not commanded: avoid sudden motion at startup.
        while not self.stop_event.is_set():
            loop_start = time.monotonic()
            moves = []
            with self.lock:
                for axis in self.axes:
                    if self.direction[axis] and loop_start > self.deadline[axis]:
                        self.direction[axis] = 0
                    sign = self.direction[axis] * NECK_SIGNS[axis]
                    if not sign:
                        continue
                    position = self.position[axis]
                    limit = NECK_MAX_OFFSET[axis]
                    next_pos = max(-limit, min(limit, position + sign * NECK_STEP))
                    if next_pos == position:
                        continue
                    # Position updates only after successful hardware dispatch.
                    moves.append((axis, next_pos))

            for axis, next_pos in moves:
                if self.stop_event.is_set():
                    break
                try:
                    # ServoCtrl.moveAngle takes an offset from init_pwm[channel].
                    self.servo.moveAngle(NECK_CHANNELS[axis], next_pos)
                    with self.lock:
                        self.position[axis] = next_pos
                    self.last_error = None
                except Exception as exc:
                    self.last_error = repr(exc)
                    print(f"[목 제어 오류] {axis}: {exc!r}")
                    # Avoid hammering a disconnected I2C device.
                    self.release_all()
                    self.stop_event.wait(0.5)
                    break

            elapsed = time.monotonic() - loop_start
            self.stop_event.wait(max(0.001, NECK_TICK - elapsed))

    def close(self):
        self.release_all()
        self.stop_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=1.0)
        # Leave each servo holding its last safe position; do not snap to neutral.


class SmoothGripper:
    """Debounce repeated grip commands and stop after heartbeat loss.

    Keeps RPIservo's own singleServo/stopWiggle mechanism. We do not issue
    per-frame gripper commands because singleServo has its own worker.
    """

    def __init__(self, servo_ctrl, speed=5):
        self.servo = servo_ctrl
        self.speed = speed
        self.direction = 0
        self.deadline = 0.0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._watchdog, name="GripSafetyWatchdog", daemon=True
        )
        self.worker.start()

    def hold(self, direction):
        if direction not in (-1, 1):
            raise ValueError("집게 방향 오류")
        with self.lock:
            self.deadline = time.monotonic() + GRIPPER_COMMAND_TIMEOUT
            if self.direction == direction:
                return   # Repeated keepalive must not restart the servo thread.
            if self.direction:
                self.servo.stopWiggle()
            self.servo.singleServo(4, direction, self.speed)
            self.direction = direction

    def release(self):
        with self.lock:
            if not self.direction:
                return
            self.direction = 0
            self.deadline = 0.0
            self.servo.stopWiggle()

    def _watchdog(self):
        while not self.stop_event.wait(0.10):
            with self.lock:
                expired = self.direction and time.monotonic() > self.deadline
            if expired:
                try:
                    self.release()
                except Exception as exc:
                    print("[집게 안전 정지 오류]", repr(exc))

    def close(self):
        self.stop_event.set()
        self.release()
        if self.worker.is_alive():
            self.worker.join(timeout=1.0)
