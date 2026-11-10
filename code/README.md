
# 🤖 PiCar-Pro AI Robot Control System

Raspberry Pi 4와 Adeept PiCar-Pro를 기반으로 제작한 AI 로봇 제어 프로젝트입니다.

PC와 스마트폰에서 로봇을 원격으로 조작할 수 있으며, 실시간 카메라 영상에 YOLO 객체 탐지를 적용했습니다.

로봇 주행, 로봇팔 및 집게 제어, OLED 눈동자 애니메이션, 방향별 라이트 제어, 음성 인식 기능을 위한 코드를 구성했습니다.

---

<img width="790" height="420" alt="사진" src="https://github.com/user-attachments/assets/4ff310de-af1b-4499-ba27-9cf3f5353d58" />

---

## 🔵 주요 기능

### 로봇 주행

다음 6가지 방향으로 로봇을 제어할 수 있습니다.

- 전진
- 후진
- 좌회전
- 우회전
- 후진 좌회전
- 후진 우회전

버튼이나 키를 누르고 있는 동안 이동하고, 손을 떼면 정지하도록 구현했습니다.

`robot_ctrl()`에서 입력된 명령을 구분하고, `drive_command()`에서 모터와 조향 Servo를 제어합니다.

### PC GUI

Tkinter를 이용해 PC에서 로봇을 제어하는 GUI입니다.

주요 기능:

- 실시간 카메라 영상 확인
- 카메라 전체화면 보기
- 전진 / 후진 / 좌우 회전
- 후진 좌우 회전
- 현재 이동 방향 및 속도 표시
- 로봇팔 및 집게 제어
- 라이트 ON / 자동 모드
- 키보드 및 마우스 조작

#### 키보드 조작

| 키 | 기능 |
|---|---|
| ↑ | 전진 |
| ↓ | 후진 |
| ← | 좌회전 |
| → | 우회전 |
| ↓ + ← | 후진 좌회전 |
| ↓ + → | 후진 우회전 |
| Z | 집게 잡기 |
| X | 집게 놓기 |
| F11 | 카메라 전체화면 |
| ESC | 프로그램 종료 |

`start_move()`와 `stop_move()`로 이동 시작과 정지를 처리하고, `resolve_keyboard_direction()`으로 여러 방향키의 조합을 해석합니다.

### 모바일 GUI

스마트폰 웹 브라우저를 이용해 로봇을 조작할 수 있도록 HTML, CSS, JavaScript 기반 GUI를 구현했습니다.

주요 기능:

- 실시간 로봇 카메라
- YOLO 객체 탐지 결과 표시
- 카메라 전체화면
- 전진 / 후진 및 좌우 회전
- 후진 좌우 회전
- STOP
- 목 관절 조작
- 집게 잡기 / 놓기
- 라이트 ON / 자동 모드
- 연결 상태 표시
- 카메라 연결 해제 시 자동 재연결
- 화면 이탈 시 주행 안전 정지
- 음성 인식 버튼 및 인식 결과 표시

모바일 주행 속도 설정:

```python
DRIVE_SPEED = 60
TURN_SPEED = 50
TURN_ANGLE = 30
```

---

## 🔵 YOLO 객체 탐지

로봇 카메라 영상에 YOLO 객체 탐지 기능을 적용했습니다.

`YOLO_detector.py`의 `YOLODetector` 클래스를 이용하여 카메라 프레임에서 객체를 인식합니다.

탐지 결과에 표시되는 정보:

- Bounding Box
- 객체 클래스 이름
- 신뢰도
- 탐지된 객체 수

### PC용 코드 설정

```python
YOLO_MODEL_PATH = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35
YOLO_IMAGE_SIZE = 224
YOLO_DETECT_EVERY_N_FRAMES = 8
```

PC용 코드는 YOLOv8n 사전학습 모델을 사용합니다.

### 모바일용 코드 설정

```python
YOLO_MODEL_PATH = "/home/pi/Adeept_PiCar-Pro/Server/models/hazard_3class_v1.pt"
YOLO_CONFIDENCE = 0.35
YOLO_IMAGE_SIZE = 224
YOLO_DETECT_EVERY_N_FRAMES = 8
```

모바일용 코드는 별도로 학습한 3클래스 객체 탐지 모델을 사용하도록 설정되어 있습니다.

### 비동기 객체 탐지

`AsyncYOLO` 클래스를 사용하여 카메라 프레임 처리와 YOLO 추론을 분리했습니다.

```text
카메라 영상 입력
       ↓
OpenCV 이미지 변환
       ↓
최신 프레임 저장
       ↓
AsyncYOLO
       ↓
YOLODetector
       ↓
객체 탐지
       ↓
Bounding Box 및 객체 정보 표시
```

모든 프레임에서 추론을 실행하는 대신 일정한 프레임 간격으로 탐지하고, 중간 프레임에는 이전 탐지 결과를 표시합니다.

이 방식으로 객체 탐지에 의한 영상 처리 부담을 줄이도록 구성했습니다.

---

## 🔵 OLED 눈동자 애니메이션

SSD1306 128×64 OLED를 이용해 로봇의 얼굴을 표현했습니다.

기본 설정:

```python
OLEDEyes(port=1, address=0x3C)
```

이동 방향에 따라 OLED 눈동자의 위치가 변경됩니다.

| 로봇 동작 | 눈동자 방향 |
|---|---|
| 정지 | 정면 |
| 전진 | 위 |
| 후진 | 아래 |
| 좌회전 | 왼쪽 |
| 우회전 | 오른쪽 |
| 후진 좌회전 | 왼쪽 아래 |
| 후진 우회전 | 오른쪽 아래 |

주요 구현 기능:

- 눈동자 방향 변경
- 웃는 얼굴 표현
- 자동 눈 깜빡임
- 눈동자 움직임 애니메이션
- 로봇 동작과 OLED 연계

`OLEDEyes` 클래스가 화면 출력을 담당하고, `set_direction()`으로 현재 주행 방향을 전달합니다.

`_draw_face()`는 얼굴을 그리고 `_update_blink()`는 눈 깜빡임 상태를 처리합니다.

---

## 🔵 라이트 제어

라이트는 수동 ON과 자동 제어 모드로 구성했습니다.

### LIGHT ON

이동 방향과 관계없이 양쪽 라이트를 계속 켭니다.

```text
왼쪽  : ON
오른쪽: ON
```

### LIGHT OFF / 자동 모드

이동 방향에 따라 라이트가 변경됩니다.

| 이동 상태 | 라이트 |
|---|---|
| 전진 | 양쪽 ON |
| 후진 | 양쪽 ON |
| 좌회전 | 왼쪽 ON |
| 우회전 | 오른쪽 ON |
| 후진 좌회전 | 왼쪽 ON |
| 후진 우회전 | 오른쪽 ON |
| 정지 | OFF |

주요 함수:

- `both_lights_on()`
- `both_lights_off()`
- `apply_motion_lights()`
- `light_on_mode()`
- `light_off_mode()`

`apply_motion_lights()`에서 현재 이동 방향에 따른 라이트 출력을 결정합니다.

---

## 🔵 로봇팔 및 집게

로봇팔과 집게를 조작하기 위한 Servo 제어 코드를 구현했습니다.

PC 제어 코드에서 지원하는 명령은 다음과 같습니다.

| 부위 | 동작 |
|---|---|
| 로봇팔 | 위 / 아래 / 정지 |
| 손목 관절 | 위 / 아래 / 정지 |
| 집게 | 잡기 / 놓기 / 정지 |
| 초기 자세 | 로봇팔·손목·집게 초기 위치 이동 |

집게는 PC에서 다음 키로도 조작할 수 있습니다.

```text
Z : 잡기
X : 놓기
```

### 집게 제어

주요 함수:

```python
gripper_start(direction)
gripper_stop()
```

`gripper_start()`는 집게 Servo에 이동 명령을 전달하고, `gripper_stop()`은 집게 동작을 정지합니다.

### 로봇팔 및 손목

PC 제어 코드의 `robot_ctrl()`에서 다음 명령을 처리합니다.

```text
armup    : 로봇팔 올리기
armdown  : 로봇팔 내리기
armstop  : 로봇팔 정지

handup   : 손목 올리기
handdown : 손목 내리기
HAstop   : 손목 정지

grab     : 집게 잡기
loose    : 집게 놓기
stop     : 집게 정지

home     : 초기 자세
```

실제 Servo 출력에는 `singleServo()`, `stopWiggle()`, `moveServoInit()` 등의 함수를 이용합니다.

---

## 🔵 음성 인식 기능

모바일 GUI에 음성 인식 버튼과 음성 인식 결과를 표시하는 기능을 추가했습니다.

음성 입력 장치로 Raspberry Pi에 연결된 **ReSpeaker Lite**를 사용하도록 구성했습니다.

### 음성 인식 동작 방식

1. 사용자가 모바일 화면에서 음성 버튼을 누릅니다.
2. 버튼을 누르고 있는 동안 녹음을 요청합니다.
3. 버튼에서 손을 떼면 녹음을 종료합니다.
4. 별도의 음성 인식 기능에서 음성을 텍스트로 변환합니다.
5. 인식 결과를 모바일 화면에 표시하도록 구성했습니다.

### 음성 인식 처리 구조

```text
음성 버튼 입력
       ↓
녹음 시작
       ↓
ReSpeaker Lite
       ↓
음성 데이터 수집
       ↓
Whisper 기반 음성 인식
       ↓
텍스트 변환
       ↓
모바일 GUI에 결과 표시
```

### 주요 기능

- 음성 인식 시작 버튼
- 누르고 있는 동안 녹음 요청
- 손을 떼면 녹음 종료
- 음성 인식 상태 표시
- 인식 결과 텍스트 표시
- 음성 인식 기능과 모바일 GUI 연결

### 관련 코드

| 함수 | 기능 |
|---|---|
| `start_voice()` | 음성 인식 기능 실행 요청 |
| `voice_request()` | 음성 처리 기능에 요청 전달 |
| `voiceAction()` | 모바일 버튼의 음성 조작 요청 |
| `refreshVoice()` | 음성 인식 상태 및 결과 갱신 |

음성 인식 기능은 `PiVoice.py`, `PiVoiceLauncher.py`와 연동하도록 작성되었습니다.

해당 파일은 현재 코드 폴더에 포함되어 있지 않으므로 별도로 준비해야 합니다.

**현재 상태:** 음성 버튼과 음성 처리 연동 코드는 구현되어 있지만, ReSpeaker Lite를 통한 실제 음성 인식 및 음성 명령 기반 로봇 제어는 추가 검증이 필요합니다.

---

## 🔵 카메라

`Picamera2`와 OpenCV를 이용하여 로봇 카메라 영상을 처리합니다.

기본 영상 설정:

```python
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
```

카메라에서 입력받은 영상은 OpenCV 형식으로 변환한 후 YOLO 탐지 결과를 표시합니다.

### PC 카메라 처리

```text
카메라 영상
     ↓
Picamera2
     ↓
OpenCV
     ↓
YOLO 객체 탐지
     ↓
JPEG 변환
     ↓
PC GUI
```

### 모바일 카메라 처리

```text
카메라 영상
     ↓
Picamera2
     ↓
OpenCV
     ↓
YOLO 객체 탐지
     ↓
JPEG 변환
     ↓
모바일 GUI
```

주요 함수:

- `camera_stream()` : PC용 카메라 영상 처리
- `camera_worker()` : 모바일용 카메라 프레임 처리
- `video_receiver()` : PC GUI 영상 수신
- `update_camera_gui()` : PC GUI 카메라 화면 갱신
- `get_latest_camera_frame()` : 가장 최근 카메라 프레임 반환

---

# 파일 구성

현재 코드 폴더는 다음과 같이 구성되어 있습니다.

```text
code/
│
├── GUIServer_custom.py
├── GUIServer_mobile.py
├── robot_gui.py
├── fullscreen_gui.py
├── YOLO_detector.py
├── OLED_eyes.py
│
└── README.md
```

### `GUIServer_custom.py`

PC 로봇 제어 기능을 처리하는 코드입니다.

- 주행 명령 처리
- 로봇팔 및 집게 제어
- 라이트 제어
- OLED 상태 전달
- 카메라 영상 처리
- YOLO 객체 탐지

### `GUIServer_mobile.py`

모바일 GUI와 로봇 제어 기능을 연결하는 코드입니다.

- 모바일 GUI 구성
- 주행 명령 처리
- 목 관절 및 집게 조작
- OLED 및 라이트 제어
- 카메라 영상 처리
- YOLO 객체 탐지
- 음성 인식 버튼과 처리 기능 연동

### `robot_gui.py`

Tkinter 기반 PC GUI 코드입니다.

- 이동 버튼 및 키보드 이벤트
- 로봇 주행 및 정지
- 집게 조작
- 라이트 ON/OFF
- 카메라 화면 표시
- 이동 상태 및 속도 표시

### `fullscreen_gui.py`

PC 카메라 전체화면 GUI 코드입니다.

- 카메라 영상 확대 표시
- 로봇 이동 조작
- 집게 제어
- 라이트 제어
- 전체화면 열기 및 종료

### `YOLO_detector.py`

YOLO 객체 탐지 코드입니다.

- YOLO 모델 로딩
- 카메라 프레임 추론
- Bounding Box 표시
- 객체 클래스 및 신뢰도 표시

### `OLED_eyes.py`

OLED 눈동자 애니메이션 코드입니다.

- 눈동자 방향 변경
- 눈 깜빡임
- 웃는 얼굴 표현
- 현재 로봇 이동 방향 연계

### 기타 의존 모듈

실제 하드웨어 제어와 음성 기능에는 다음 모듈이 추가로 필요합니다.

```text
Move.py
RPIservo.py
Switch.py
SmoothServo.py
PiVoice.py
PiVoiceLauncher.py
```

일부 모듈은 현재 코드 폴더에 포함되어 있지 않습니다.

---

# 사용 환경

## Hardware

- Raspberry Pi 4
- Adeept PiCar-Pro
- Raspberry Pi에 연결된 카메라
- DC Motor
- Steering Servo
- Robot Gripper Servo
- LED Light
- SSD1306 128×64 OLED
- ReSpeaker Lite (음성 인식 기능용)

## Software

- Python 3
- OpenCV
- Picamera2
- Tkinter
- ZeroMQ
- Ultralytics YOLO
- PyTorch
- Pillow
- NumPy
- luma.oled
- Whisper 기반 음성 인식 라이브러리

---

# 구현 내용

기존 PiCar-Pro 제어 기능을 확장하여 다음과 같은 코드를 구성했습니다.

- 전진 / 후진 / 좌우 회전
- 후진 좌회전 / 후진 우회전
- PC용 Tkinter GUI
- 모바일 웹 GUI
- 누르고 있는 동안만 이동하는 조작 방식
- 실시간 카메라 영상 처리
- 카메라 전체화면
- YOLO 객체 탐지
- Bounding Box 및 탐지 정보 표시
- 카메라와 YOLO 비동기 처리
- OLED 눈동자 애니메이션
- 주행 방향에 따른 OLED 표현
- OLED 자동 눈 깜빡임
- 로봇팔 및 집게 제어
- 이동 방향에 따른 LED 조명 제어
- 모바일 음성 인식 버튼 및 결과 표시 코드
- 동작 정지 및 안전 정지 처리

---

# 향후 개선 방향

- 객체 자동 추적
- 카메라 기반 장애물 회피
- 로봇팔을 이용한 객체 자동 수거
- 작업 구역별 객체 분류
- 음성 인식 실기 테스트 및 명령어 연동
- 자율주행 기능
- 인식된 객체 위치에 따른 로봇 행동 자동화

---

# 주요 클래스 및 함수 정리

각 파일에서 사용되는 주요 클래스와 함수는 다음과 같습니다.

## 1. 로봇 주행

| 클래스 / 함수 | 파일 | 역할 |
|---|---|---|
| `start_move()` | `robot_gui.py` | 이동 시작 |
| `stop_move()` | `robot_gui.py` | 이동 정지 |
| `resolve_keyboard_direction()` | `robot_gui.py` | 방향키 조합 판별 |
| `update_keyboard_movement()` | `robot_gui.py` | 키보드 상태에 따른 주행 변경 |
| `key_pressed()` | `robot_gui.py` | 키 입력 처리 |
| `key_released()` | `robot_gui.py` | 키 해제 처리 |
| `robot_ctrl()` | `GUIServer_custom.py`, `GUIServer_mobile.py` | 주행·관절·집게 명령 분류 |
| `drive_command()` | `GUIServer_custom.py`, `GUIServer_mobile.py` | 모터·조향 및 조명 연계 |
| `safe_stop()` | `GUIServer_custom.py`, `GUIServer_mobile.py` | 모터 정지 및 조향 복귀 |
| `move.move()` | `Move.py` | 바퀴 모터 구동 |
| `move.motorStop()` | `Move.py` | 바퀴 모터 정지 |

## 2. 로봇팔 및 목 관절

| 부위 | 클래스 / 함수 | 기능 |
|---|---|---|
| 앞바퀴 조향 | `steering.moveAngle()` | Servo 채널 0 조향 |
| 로봇팔 상하 | `arm_servo.singleServo()` | Servo 채널 2 |
| 손목 상하 | `hand_servo.singleServo()` | Servo 채널 3 |
| 로봇팔·손목 정지 | `stopWiggle()` | Servo 동작 정지 |
| 초기 자세 | `moveServoInit()` | Servo 초기 위치로 이동 |
| 모바일 목 관절 | `SmoothNeck`, `neck_controller.command()` | 목 A/B 관절 제어 |

## 3. 집게

| 클래스 / 함수 | 역할 |
|---|---|
| `arm_grab_start()` | PC 집게 잡기 시작 |
| `arm_release_start()` | PC 집게 놓기 시작 |
| `arm_stop()` | PC 집게 정지 |
| `gripper_start()` | 집게 Servo 동작 시작 |
| `gripper_stop()` | 집게 Servo 동작 정지 |
| `singleServo()` | Servo 연속 이동 |
| `stopWiggle()` | Servo 연속 이동 중단 |
| `SmoothGripper` | 모바일 집게 조작 제어 |

## 4. 라이트

| 함수 | 역할 |
|---|---|
| `flash_on()` | PC GUI 라이트 ON |
| `flash_off()` | PC GUI 자동 모드 |
| `both_lights_on()` | 양쪽 라이트 켜기 |
| `both_lights_off()` | 양쪽 라이트 끄기 |
| `apply_motion_lights()` | 이동 방향별 점등 |
| `light_on_mode()` | 상시 점등 모드 |
| `light_off_mode()` | 자동 방향등 모드 |

## 5. OLED

| 클래스 / 함수 | 역할 |
|---|---|
| `OLEDEyes` | OLED 애니메이션 전체 관리 |
| `set_direction()` | 눈동자 방향 설정 |
| `_direction_vector()` | 방향별 눈동자 이동값 계산 |
| `_update_blink()` | 눈 깜빡임 처리 |
| `_draw_face()` | 얼굴 및 눈동자 그리기 |
| `run()` | OLED 애니메이션 실행 |
| `stop()` | OLED 애니메이션 종료 |

## 6. 카메라 및 YOLO

| 클래스 / 함수 | 역할 |
|---|---|
| `camera_stream()` | PC용 카메라 영상 처리 |
| `camera_worker()` | 모바일 카메라 영상 처리 |
| `video_receiver()` | PC GUI 영상 수신 |
| `update_camera_gui()` | PC GUI 화면 갱신 |
| `FullscreenGUI` | 카메라 전체화면 GUI |
| `YOLODetector` | YOLO 객체 탐지 클래스 |
| `_run_inference()` | 실제 YOLO 추론 |
| `_draw_detections()` | 탐지 결과 표시 |
| `AsyncYOLO` | 비동기 YOLO 처리 |
| `submit()` | YOLO 탐지용 최신 프레임 제출 |
| `draw()` | 최근 탐지 결과 표시 |

## 7. 음성 인식

| 함수 | 역할 |
|---|---|
| `start_voice()` | 음성 인식 기능 실행 요청 |
| `voice_request()` | 음성 처리 요청 및 결과 수신 |
| `voiceAction()` | 음성 버튼 입력 처리 |
| `refreshVoice()` | 인식 상태 및 결과 표시 |

---

## Project Goal

카메라로 주변 환경을 인식하고 PC 또는 스마트폰에서 로봇을 조작할 수 있는 AI 로봇 제어 시스템을 구현하는 것을 목표로 합니다.

로봇 주행, 카메라 영상 처리, 객체 인식, 로봇팔과 집게 제어, OLED 및 라이트 표현 기능을 통합하고, 향후 음성 명령과 객체 탐지 결과를 실제 로봇 행동에 연결하여 자율적으로 주변 환경을 인식하고 대응할 수 있는 로봇 시스템으로 확장할 계획입니다.
