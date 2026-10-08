# 🤖 PiCar-Pro AI Robot Control System

Raspberry Pi 4와 Adeept PiCar-Pro를 기반으로 제작한 로봇 제어 프로젝트입니다.

PC와 스마트폰에서 로봇을 원격으로 조작할 수 있으며, 실시간 카메라 영상에 YOLO 객체 탐지를 적용했습니다.  
주행 방향에 따라 OLED 눈동자와 라이트가 함께 동작하며, 로봇 집게도 GUI에서 제어할 수 있습니다.

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

---

### PC GUI

Tkinter를 이용한 PC용 로봇 제어 GUI입니다.

주요 기능은 다음과 같습니다.

- 실시간 로봇 카메라 확인
- 카메라 전체화면 보기
- 전진 / 후진 / 좌우 회전
- 후진 좌우 회전
- 현재 이동 방향 및 속도 표시
- 로봇 집게 제어
- 라이트 ON / 자동 모드 전환
- 키보드 조작 지원

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

---

## 🔵 모바일 GUI

별도의 애플리케이션 설치 없이 스마트폰 웹 브라우저를 통해 로봇을 제어할 수 있습니다.

스마트폰과 Raspberry Pi가 같은 네트워크에 연결되어 있으면 다음 주소로 접속합니다.

```text
http://라즈베리파이IP:5000
```

예시:

```text
http://192.168.0.10:5000
```

### 모바일 기능

- 실시간 로봇 카메라
- YOLO 객체 탐지 결과 표시
- 카메라 전체화면
- 전진 / 후진
- 좌회전 / 우회전
- 후진 좌회전 / 후진 우회전
- STOP
- 집게 잡기 / 놓기
- 라이트 ON / 자동 모드
- 서버 연결 상태 표시
- 카메라 연결 해제 시 자동 재연결
- 화면 이탈 시 주행 안전 정지

모바일 주행 속도는 현재 다음과 같이 설정되어 있습니다.

```text
전진 / 후진 : 60
좌회전 / 우회전 : 50
```

---

## 🔵 YOLO 객체 탐지

로봇 카메라 영상에 YOLO 객체 탐지를 적용했습니다.

사용 모델:

```text
YOLOv8n
```

YOLOv8n 사전학습 모델에 포함된 전체 클래스를 대상으로 탐지하며 특정 클래스만 제한하지 않습니다.

탐지된 객체에는 다음 정보가 표시됩니다.

- Bounding Box
- 객체 클래스 이름
- 신뢰도
- 현재 탐지된 객체 수

기본 설정:

```python
YOLO_MODEL_PATH = "yolov8n.pt"
YOLO_CONFIDENCE = 0.35
YOLO_IMAGE_SIZE = 320
YOLO_DETECT_EVERY_N_FRAMES = 3
```

Raspberry Pi 4의 성능을 고려하여 모든 프레임에서 YOLO 추론을 실행하지 않고 일정 프레임 간격으로 객체 탐지를 실행합니다.

탐지하지 않는 중간 프레임에서는 이전 탐지 결과를 다시 표시해 영상 속도 저하를 줄였습니다.

---

## 🔵 OLED 눈동자

SSD1306 128×64 OLED를 이용해 로봇의 얼굴을 표현합니다.

기본 연결 설정:

```text
I2C Port : 1
Address  : 0x3C
```

로봇의 움직임에 따라 눈동자의 방향이 변경됩니다.

| 로봇 상태 | OLED 동작 |
|---|---|
| 정지 | 정면 |
| 전진 | 위 |
| 후진 | 아래 |
| 좌회전 | 왼쪽 |
| 우회전 | 오른쪽 |
| 후진 좌회전 | 왼쪽 아래 |
| 후진 우회전 | 오른쪽 아래 |

정지 상태에서도 웃는 얼굴을 유지하며 약 3초 간격으로 자동 눈 깜빡임 애니메이션이 실행됩니다.

---

## 🔵 라이트 제어

라이트는 두 가지 모드로 동작합니다.

### LIGHT ON

이동 방향과 관계없이 양쪽 라이트를 계속 켭니다.

```text
왼쪽  : ON
오른쪽: ON
```

### LIGHT OFF / 자동 모드

LIGHT OFF는 라이트를 완전히 사용하지 않는 기능이 아니라 이동 방향에 따라 자동으로 라이트를 제어하는 모드입니다.

| 이동 상태 | 라이트 |
|---|---|
| 전진 | 양쪽 ON |
| 후진 | 양쪽 ON |
| 좌회전 | 왼쪽 ON |
| 우회전 | 오른쪽 ON |
| 후진 좌회전 | 왼쪽 ON |
| 후진 우회전 | 오른쪽 ON |
| 정지 | OFF |

---

## 🔵 로봇 집게

PC와 모바일에서 로봇 집게를 제어할 수 있습니다.

```text
잡기
놓기
```

버튼을 누르고 있는 동안 Servo가 움직이고 버튼에서 손을 떼면 Servo 동작이 정지합니다.

PC에서는 다음 키도 사용할 수 있습니다.

```text
Z : 잡기
X : 놓기
```

---

## 🔵 카메라

Raspberry Pi Camera와 `Picamera2`를 사용합니다.

기본 영상 크기:

```text
640 × 480
```

카메라에서 받은 영상을 OpenCV 형식으로 변환한 뒤 YOLO 객체 탐지를 수행하고 GUI로 전송합니다.

### PC

```text
Raspberry Pi Camera
        ↓
Picamera2
        ↓
OpenCV
        ↓
YOLOv8n
        ↓
JPEG
        ↓
ZeroMQ
        ↓
robot_gui.py
        ↓
PC GUI
```

### 모바일

```text
Raspberry Pi Camera
        ↓
Picamera2
        ↓
OpenCV
        ↓
YOLOv8n
        ↓
JPEG
        ↓
MJPEG Stream
        ↓
Web Browser
```

---

# 파일 구성

```text
Project/
│
├── GUIServer_custom.py
├── GUIServer_mobile.py
├── robot_gui.py
├── fullscreen_gui.py
├── YOLO_detector.py
├── OLED_eyes.py
│
├── Move.py
├── RPIservo.py
├── Switch.py
│
└── yolov8n.pt
```

### `GUIServer_custom.py`

PC GUI용 Raspberry Pi 서버입니다.

주요 역할:

- 로봇 이동 명령 처리
- Servo 제어
- 집게 제어
- 라이트 제어
- OLED 상태 전달
- Raspberry Pi 카메라 실행
- YOLO 객체 탐지
- PC GUI로 카메라 영상 전송

PC GUI와 TCP `10223` 포트로 통신하며 카메라 영상은 `5555` 포트를 사용합니다.

---

### `robot_gui.py`

PC용 Tkinter GUI입니다.

주요 역할:

- 서버 연결
- 실시간 카메라 표시
- 주행 제어
- 키보드 제어
- 속도 표시
- 집게 제어
- 라이트 제어
- 전체화면 GUI 호출

---

### `fullscreen_gui.py`

PC 카메라 전체화면 GUI입니다.

`robot_gui.py`에서 전달받은 카메라 영상과 로봇 제어 함수를 사용합니다.

전체화면에서도 다음 기능을 사용할 수 있습니다.

- 실시간 카메라
- 주행
- 정지
- 집게
- 라이트

`ESC` 또는 `F11`을 누르면 전체화면에서 나올 수 있습니다.

---

### `GUIServer_mobile.py`

스마트폰 웹 브라우저용 서버입니다.

별도의 앱 설치 없이 Raspberry Pi의 IP 주소로 접속하여 사용할 수 있습니다.

주요 역할:

- 모바일 웹 GUI 제공
- 로봇 주행 제어
- 집게 제어
- 라이트 제어
- OLED 제어
- 실시간 카메라 MJPEG 스트리밍
- YOLO 객체 탐지
- 카메라 전체화면 지원

---

### `YOLO_detector.py`

카메라 프레임의 객체를 탐지하는 모듈입니다.

```text
OpenCV Frame
      ↓
YOLOv8n
      ↓
Object Detection
      ↓
Bounding Box + Class + Confidence
```

---

### `OLED_eyes.py`

OLED 얼굴 애니메이션을 담당합니다.

로봇의 현재 이동 방향을 전달받아 눈동자의 위치를 변경하고 자동 깜빡임 및 얼굴 애니메이션을 출력합니다.

---

### Adeept 기본 모듈

다음 파일들은 PiCar-Pro 하드웨어 제어에 사용됩니다.

```text
Move.py
RPIservo.py
Switch.py
```

Adeept PiCar-Pro의 기존 제어 모듈을 사용합니다.

---

# 사용 환경

## Hardware

- Raspberry Pi 4
- Adeept PiCar-Pro
- Raspberry Pi Camera
- DC Motor
- Steering Servo
- Robot Gripper Servo
- LED Light
- SSD1306 128×64 OLED

## Software

- Raspberry Pi OS
- Python 3
- OpenCV
- Picamera2
- Tkinter
- ZeroMQ
- Ultralytics YOLO
- Pillow
- NumPy
- luma.oled

---

# Python 라이브러리

프로젝트에서 사용하는 주요 라이브러리는 다음과 같습니다.

```bash
pip3 install ultralytics
pip3 install pyzmq
pip3 install pillow
pip3 install numpy
pip3 install luma.oled
```

OpenCV와 Picamera2는 Raspberry Pi 환경에 맞게 설치합니다.

예시:

```bash
sudo apt update
sudo apt install python3-opencv python3-picamera2
```

---

# 실행 방법

프로젝트 파일을 Adeept PiCar-Pro Server 폴더에 위치시킵니다.

예시:

```bash
cd ~/Adeept_PiCar-Pro/Server
```

---

## PC GUI 실행

### 1. Raspberry Pi 서버 실행

```bash
sudo python3 GUIServer_custom.py
```

### 2. PC GUI 실행

새 터미널에서:

```bash
python3 robot_gui.py
```

현재 `robot_gui.py`의 기본 서버 주소는 다음과 같습니다.

```python
SERVER_IP = "127.0.0.1"
SERVER_PORT = 10223
VIDEO_PORT = 5555
```

Raspberry Pi 자체 데스크톱이나 VNC에서 실행할 경우 `127.0.0.1`을 사용할 수 있습니다.

다른 PC에서 직접 GUI를 실행한다면 다음 부분을 Raspberry Pi의 IP 주소로 변경해야 합니다.

```python
SERVER_IP = "라즈베리파이 IP"
```

---

## 모바일 GUI 실행

PC용 서버가 실행 중이라면 먼저 종료합니다.

모바일 서버 실행:

```bash
cd ~/Adeept_PiCar-Pro/Server
sudo python3 GUIServer_mobile.py
```

스마트폰을 Raspberry Pi와 같은 네트워크에 연결합니다.

이후 브라우저에서 다음 주소를 입력합니다.

```text
http://라즈베리파이IP:5000
```

예시:

```text
http://192.168.0.10:5000
```

---

# PC / 모바일 서버 사용 시 주의

다음 두 서버는 동시에 실행하지 않습니다.

```text
GUIServer_custom.py
GUIServer_mobile.py
```

PC GUI를 사용할 경우:

```bash
sudo python3 GUIServer_custom.py
```

모바일 GUI를 사용할 경우:

```bash
sudo python3 GUIServer_mobile.py
```

사용할 환경에 맞는 서버 하나만 실행합니다.

---

# 시스템 구성

```text
                    ┌─────────────────┐
                    │ Raspberry Pi 4  │
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
          ▼                  ▼                  ▼
       Camera              Motor              OLED
          │                  │                  │
          ▼                  ▼                  ▼
     Picamera2           Move.py          OLED_eyes.py
          │
          ▼
       OpenCV
          │
          ▼
      YOLOv8n
          │
          ├─────────────────────────────┐
          │                             │
          ▼                             ▼
 GUIServer_custom.py            GUIServer_mobile.py
          │                             │
          ▼                             ▼
    robot_gui.py                 Mobile Browser
          │
          ▼
 fullscreen_gui.py
```

---

# 구현 내용

이 프로젝트에서는 기존 PiCar-Pro의 기본 이동 기능에 추가하여 다음 기능을 구현했습니다.

- 후진 좌회전 / 후진 우회전
- PC용 로봇 제어 GUI
- 모바일 웹 기반 로봇 제어 GUI
- 버튼을 누르는 동안만 이동하는 방식
- 실시간 Raspberry Pi 카메라 스트리밍
- 카메라 전체화면 기능
- YOLOv8n 실시간 객체 탐지
- 탐지 객체 Bounding Box 표시
- 객체 이름 및 신뢰도 표시
- OLED 눈동자 애니메이션
- 이동 방향에 따른 OLED 표현
- 자동 눈 깜빡임
- 로봇 집게 제어
- 수동 라이트 ON
- 이동 방향 기반 자동 라이트
- 프로그램 종료 및 연결 해제 시 안전 정지 처리

---

# 향후 개선 방향

현재 기능을 기반으로 다음 기능을 추가할 수 있습니다.

- 사용자 정의 YOLO 모델 추가
- YOLO 기본 객체와 직접 학습한 객체 동시 탐지
- 객체 자동 추적
- 카메라 기반 장애물 회피
- 로봇 팔을 이용한 객체 자동 수거
- 작업 구역별 객체 분류
- 환경 카메라 연결
- 음성 명령 기능
- 자율주행 기능
- 감지된 객체 위치 기반 로봇 행동 자동화

---

## Project Goal

카메라로 주변 환경을 인식하고 사용자가 PC 또는 스마트폰에서 원격으로 로봇을 제어할 수 있는 시스템을 구현하는 것을 목표로 합니다.

향후 객체 탐지 결과와 로봇의 주행 및 로봇 팔 동작을 연결하여 단순 원격 조종을 넘어 주변 환경을 인식하고 행동할 수 있는 로봇 시스템으로 확장할 예정입니다.
