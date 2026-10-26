#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Attach a microphone button and transcription text to an existing Tkinter GUI."""
import json
import queue
import threading
import time
import tkinter as tk
from urllib.request import Request, urlopen

URL = "http://127.0.0.1:5056"


def attach_voice_panel(window, header, header_left):
    events = queue.Queue()
    actions = queue.Queue()
    held = {"value": False, "pending_release": None}
    stopping = threading.Event()

    panel = tk.Frame(header, bg="#FFFFFF", padx=8, pady=4)
    panel.pack(side="right", padx=(0, 10))

    button = tk.Label(panel, text="🎤 누르고 말하기", bg="#3182F6", fg="#FFFFFF",
                      font=("Malgun Gothic", 11, "bold"), padx=16, pady=12,
                      cursor="hand2")
    button.pack()
    status_label = tk.Label(header_left, text="음성: 연결 확인 중", bg=header_left.cget("bg"),
                            fg="#52749C", font=("Malgun Gothic", 10), anchor="w")
    status_label.pack(anchor="w", pady=(3, 0))
    result_label = tk.Label(header_left, text="", bg=header_left.cget("bg"),
                            fg="#263646", font=("Malgun Gothic", 10),
                            anchor="w", justify="left", wraplength=500)
    result_label.pack(anchor="w")

    def request(action):
        req = Request(URL + "/" + action,
                      method="GET" if action == "status" else "POST")
        try:
            with urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read(12000).decode("utf-8"))
        except Exception as exc:
            return {"state": "offline", "message": "음성 서비스 연결 실패", "error": str(exc)}

    def action_worker():
        while not stopping.is_set():
            try:
                action = actions.get(timeout=.25)
            except queue.Empty:
                continue
            if action is None:
                break
            events.put(request(action))

    def status_worker():
        while not stopping.wait(.55):
            if held["value"]:
                actions.put("ping")
            events.put(request("status"))

    def render(status):
        state = status.get("state", "offline")
        messages = {
            "loading": "음성: AI 모델 준비 중",
            "opening": "음성: 마이크 연결 중",
            "recording": "음성: 녹음 중 (놓으면 분석)",
            "processing": "음성: 한국어 인식 중",
            "ready": "음성: 🎤 누르거나 Enter 키를 누르고 말하세요",
            "done": "음성: 인식 완료",
            "error": "음성: 오류",
            "offline": "음성: 서비스 연결 대기 중",
        }
        status_label.configure(text=messages.get(state, "음성: " + state))
        if state == "recording":
            button.configure(text="● 녹음 중", bg="#D9534F")
        else:
            button.configure(text="🎤 누르고 말하기", bg="#3182F6")
        if state == "done" and status.get("text"):
            sec = status.get("elapsed")
            tail = f" ({sec}초)" if sec is not None else ""
            result_label.configure(text="인식: " + status["text"] + tail)
        elif state in ("error", "offline"):
            result_label.configure(text=status.get("error", ""))

    def pump():
        if stopping.is_set():
            return
        latest = None
        try:
            while True:
                latest = events.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            render(latest)
        window.after(160, pump)

    def start_record(event=None):
        if not held["value"]:
            held["value"] = True
            if held["pending_release"] is not None:
                window.after_cancel(held["pending_release"])
                held["pending_release"] = None
            actions.put("start")
        return "break"

    def stop_record(event=None):
        if held["value"]:
            held["value"] = False
            actions.put("stop")
        return "break"

    def key_press(event):
        if held["pending_release"] is not None:
            window.after_cancel(held["pending_release"])
            held["pending_release"] = None
            return "break"
        return start_record(event)

    def finish_key_release():
        held["pending_release"] = None
        stop_record()

    def key_release(event):
        if held["value"] and held["pending_release"] is None:
            held["pending_release"] = window.after(90, finish_key_release)
        return "break"

    button.bind("<ButtonPress-1>", start_record)
    button.bind("<ButtonRelease-1>", stop_record)
    button.bind("<Leave>", stop_record)
    for symbol in ("Return", "KP_Enter"):
        window.bind_all(f"<KeyPress-{symbol}>", key_press, add="+")
        window.bind_all(f"<KeyRelease-{symbol}>", key_release, add="+")
    window.bind("<FocusOut>", lambda e: stop_record(), add="+")

    # The main robot application still owns its normal exit procedure.
    def closing():
        stop_record()
        stopping.set()
        actions.put(None)
    window.bind("<Destroy>", lambda e: closing() if e.widget is window else None, add="+")

    threading.Thread(target=action_worker, daemon=True, name="VoiceActions").start()
    threading.Thread(target=status_worker, daemon=True, name="VoiceStatus").start()
    window.after(200, pump)
