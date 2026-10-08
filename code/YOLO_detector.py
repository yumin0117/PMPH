#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
YOLO_detector.py

PiCar-Pro 카메라 영상용 YOLO 객체 탐지 모듈
- Raspberry Pi 카메라 프레임(OpenCV BGR)을 입력받음
- YOLOv8n 사전학습 모델 사용
- YOLO가 학습한 전체 클래스 대상으로 탐지
- 특정 classes 필터 없음
- 탐지 객체에 사각형 + 클래스 이름 + 신뢰도 표시
- Raspberry Pi 4에서 영상이 너무 느려지는 것을 줄이기 위해
  DETECT_EVERY_N_FRAMES 간격으로 추론하고, 중간 프레임에는 마지막 박스를 다시 그림
"""

import cv2
from ultralytics import YOLO


class YOLODetector:
    def __init__(
        self,
        model_path="yolov8n.pt",
        confidence=0.35,
        imgsz=256,
        detect_every_n_frames=5,
    ):
        self.model_path = model_path
        self.confidence = confidence
        self.imgsz = imgsz
        self.detect_every_n_frames = max(1, int(detect_every_n_frames))

        self.frame_count = 0
        self.last_detections = []

        print("[YOLO] 모델 로딩 중:", self.model_path)
        self.model = YOLO(self.model_path)
        print("[YOLO] 모델 로드 완료")
        print("[YOLO] 클래스 수:", len(self.model.names))

    def _run_inference(self, frame_bgr):
        result = self.model.predict(
            source=frame_bgr,
            conf=self.confidence,
            imgsz=self.imgsz,
            verbose=False,
        )[0]

        detections = []

        if result.boxes is None:
            return detections

        for box in result.boxes:
            x1, y1, x2, y2 = (
                box.xyxy[0]
                .cpu()
                .numpy()
                .astype(int)
            )

            confidence = float(box.conf[0])
            class_id = int(box.cls[0])
            class_name = self.model.names[class_id]

            detections.append(
                (
                    x1,
                    y1,
                    x2,
                    y2,
                    class_name,
                    confidence,
                )
            )

        return detections

    def _draw_detections(self, frame_bgr, detections):
        for (
            x1,
            y1,
            x2,
            y2,
            class_name,
            confidence,
        ) in detections:
            color = (0, 255, 0)

            cv2.rectangle(
                frame_bgr,
                (x1, y1),
                (x2, y2),
                color,
                2,
            )

            label = f"{class_name} {confidence:.2f}"

            (text_width, text_height), baseline = cv2.getTextSize(
                label,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                2,
            )

            label_top = max(
                y1 - text_height - baseline - 8,
                0,
            )

            cv2.rectangle(
                frame_bgr,
                (x1, label_top),
                (x1 + text_width + 8, y1),
                color,
                -1,
            )

            cv2.putText(
                frame_bgr,
                label,
                (x1 + 4, max(y1 - 6, 14)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 0, 0),
                2,
                cv2.LINE_AA,
            )

        cv2.putText(
            frame_bgr,
            f"YOLO Objects: {len(detections)}",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        return frame_bgr

    def detect_and_draw(self, frame_bgr):
        self.frame_count += 1

        if (
            self.frame_count == 1
            or self.frame_count % self.detect_every_n_frames == 0
        ):
            try:
                self.last_detections = self._run_inference(frame_bgr)
            except Exception as e:
                print("[YOLO] 탐지 오류:", repr(e))

        return self._draw_detections(
            frame_bgr,
            self.last_detections,
        )
