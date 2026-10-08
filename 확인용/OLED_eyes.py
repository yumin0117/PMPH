#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OLED_eyes.py

Adeept PiCar-Pro / Raspberry Pi용 OLED 눈동자 애니메이션 모듈
- SSD1306 128x64 OLED
- I2C port 1
- address 0x3C
- 로봇 동작 상태에 따라 눈동자가 이동
- 정지 상태에서도 웃는 얼굴 유지
- 약 3초마다 자동 깜빡임

지원 상태:
stop
forward
backward
left
right
backleft
backright
"""

import threading
import time
import math

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306


class OLEDEyes(threading.Thread):
    def __init__(self, port=1, address=0x3C):
        super().__init__(daemon=True)

        self.port = port
        self.address = address

        self.running = True
        self.connected = False

        self.direction = "stop"

        self.frame_count = 0

        self.last_blink = time.time()
        self.blinking = False
        self.blink_until = 0.0

        self.lock = threading.Lock()

        try:
            serial = i2c(
                port=self.port,
                address=self.address
            )

            self.device = ssd1306(
                serial,
                rotate=0
            )

            self.connected = True

            print(
                f"[OLED] 연결 성공 - "
                f"I2C {self.port}, "
                f"주소 0x{self.address:02X}"
            )

        except Exception as e:
            self.device = None
            self.connected = False

            print(
                "[OLED] 연결 실패:",
                e
            )

    # =====================================================
    # 외부에서 로봇 상태 전달
    # =====================================================
    def set_direction(self, direction):
        allowed = {
            "stop",
            "forward",
            "backward",
            "left",
            "right",
            "backleft",
            "backright",
        }

        if direction not in allowed:
            direction = "stop"

        with self.lock:
            self.direction = direction

    # =====================================================
    # 방향 벡터
    # =====================================================
    def _direction_vector(self, direction):
        vectors = {
            "stop": (0, 0),
            "forward": (0, -1),
            "backward": (0, 1),
            "left": (-1, 0),
            "right": (1, 0),
            "backleft": (-1, 1),
            "backright": (1, 1),
        }

        return vectors.get(
            direction,
            (0, 0)
        )

    # =====================================================
    # 눈 깜빡임
    # =====================================================
    def _update_blink(self):
        now = time.time()

        if (
            not self.blinking
            and now - self.last_blink > 3.0
        ):
            self.blinking = True
            self.blink_until = now + 0.14

        if (
            self.blinking
            and now >= self.blink_until
        ):
            self.blinking = False
            self.last_blink = now

    # =====================================================
    # 이동 중 살짝 위아래 애니메이션
    # =====================================================
    def _bob(self, direction):
        if direction == "stop":
            return 0

        return int(
            round(
                math.sin(
                    self.frame_count * 0.55
                )
            )
        )

    # =====================================================
    # 얼굴 그리기
    # =====================================================
    def _draw_face(self, draw, direction):
        self._update_blink()

        dx, dy = self._direction_vector(
            direction
        )

        eye_dx = dx * 6
        eye_dy = dy * 4

        bob = self._bob(
            direction
        )

        cy = 24 + bob

        # -------------------------------------------------
        # 눈
        # -------------------------------------------------
        if self.blinking:
            # 깜빡이는 눈
            draw.arc(
                (18, cy - 2, 54, cy + 9),
                0,
                180,
                fill="white",
                width=2
            )

            draw.arc(
                (74, cy - 2, 110, cy + 9),
                0,
                180,
                fill="white",
                width=2
            )

        else:
            # 눈 외곽
            draw.rounded_rectangle(
                (18, cy - 13, 54, cy + 13),
                radius=8,
                outline="white",
                width=2
            )

            draw.rounded_rectangle(
                (74, cy - 13, 110, cy + 13),
                radius=8,
                outline="white",
                width=2
            )

            # 눈동자
            for cx in (36, 92):
                px = cx + eye_dx
                py = cy + eye_dy

                # 흰색 눈동자 원
                draw.ellipse(
                    (
                        px - 7,
                        py - 7,
                        px + 7,
                        py + 7
                    ),
                    fill="white"
                )

                # 검은색 중심
                draw.ellipse(
                    (
                        px - 3,
                        py - 3,
                        px + 3,
                        py + 3
                    ),
                    fill="black"
                )

                # 흰 영역 좌상단의 검은 +
                plus_x = px - 4
                plus_y = py - 4

                draw.line(
                    (
                        plus_x - 1,
                        plus_y,
                        plus_x + 1,
                        plus_y
                    ),
                    fill="black",
                    width=1
                )

                draw.line(
                    (
                        plus_x,
                        plus_y - 1,
                        plus_x,
                        plus_y + 1
                    ),
                    fill="black",
                    width=1
                )

        # -------------------------------------------------
        # 코
        # -------------------------------------------------
        draw.ellipse(
            (
                61,
                cy + 9,
                67,
                cy + 13
            ),
            fill="white"
        )

        # -------------------------------------------------
        # 웃는 입
        # -------------------------------------------------
        mouth_y = 45 + bob

        if direction == "stop":
            draw.arc(
                (
                    45,
                    mouth_y - 5,
                    83,
                    mouth_y + 10
                ),
                10,
                170,
                fill="white",
                width=2
            )

        else:
            draw.arc(
                (
                    42,
                    mouth_y - 4,
                    86,
                    mouth_y + 12
                ),
                10,
                170,
                fill="white",
                width=2
            )

    # =====================================================
    # OLED Thread
    # =====================================================
    def run(self):
        if not self.connected:
            print(
                "[OLED] 장치가 없어 "
                "눈동자 Thread를 시작하지 않습니다."
            )
            return

        while self.running:
            with self.lock:
                direction = self.direction

            try:
                with canvas(
                    self.device
                ) as draw:
                    self._draw_face(
                        draw,
                        direction
                    )

            except Exception as e:
                print(
                    "[OLED] 화면 출력 오류:",
                    e
                )

            self.frame_count += 1

            # 약 12.5 FPS
            time.sleep(0.08)

    # =====================================================
    # 종료
    # =====================================================
    def stop(self):
        self.running = False

        if self.connected:
            try:
                self.device.clear()
            except Exception:
                pass


# =========================================================
# OLED만 단독 테스트할 때 사용
# =========================================================
if __name__ == "__main__":
    oled = OLEDEyes()
    oled.start()

    try:
        test_sequence = [
            ("stop", 3),
            ("forward", 2),
            ("stop", 1),
            ("backward", 2),
            ("stop", 1),
            ("left", 2),
            ("stop", 1),
            ("right", 2),
            ("stop", 1),
            ("backleft", 2),
            ("stop", 1),
            ("backright", 2),
            ("stop", 3),
        ]

        while True:
            for direction, seconds in test_sequence:
                print(
                    "[OLED TEST]",
                    direction
                )

                oled.set_direction(
                    direction
                )

                time.sleep(
                    seconds
                )

    except KeyboardInterrupt:
        print(
            "\n[OLED] 테스트 종료"
        )

    finally:
        oled.stop()
        oled.join(
            timeout=1
        )
