#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC Tk GUI 입력창. 마이크는 로봇에 연결된 장치를 서버가 사용한다."""
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def attach_command_panel(window, parent, server_ip, port=5057, on_focus=None):
    base = f'http://{server_ip}:{port}/api/ai/'
    events = queue.Queue()
    tasks = queue.Queue()
    closed = threading.Event()
    frame = tk.Frame(parent, bg='white', padx=10, pady=8)
    frame.pack(side='left', padx=12)
    tk.Label(frame, text='음성 / 텍스트 명령', bg='white').pack(anchor='w')
    row = tk.Frame(frame, bg='white')
    row.pack(fill='x', pady=4)
    entry = ttk.Entry(row, width=28)
    entry.pack(side='left', fill='x', expand=True)
    if on_focus is not None:
        entry.bind('<FocusIn>', lambda _event: on_focus())
    status = tk.StringVar(value='명령 서비스 연결 확인 중')
    text = tk.StringVar(value='에이 오른쪽으로 / 비 위로 / 앞으로 가')
    progress = ttk.Progressbar(frame, mode='indeterminate', length=270)
    progress.pack(fill='x')
    tk.Label(frame, textvariable=status, bg='white', anchor='w', wraplength=320).pack(fill='x')
    tk.Label(frame, textvariable=text, bg='white', anchor='w', wraplength=320).pack(fill='x')
    buttons = tk.Frame(frame, bg='white')
    buttons.pack(fill='x', pady=4)

    def submit(action, value=''):
        tasks.put((action, value))
        status.set('마이크/모델 준비 중' if action == 'voice' else 'api 전달중')

    def send_text(event=None):
        value = entry.get().strip()
        if value:
            submit('text', value)
        return 'break'

    send = ttk.Button(row, text='전송', command=send_text)
    send.pack(side='left', padx=4)
    voice = ttk.Button(buttons, text='로봇 마이크 · 6초', command=lambda: submit('voice'))
    voice.pack(side='left')
    cancel = ttk.Button(buttons, text='AI 중지', command=lambda: submit('cancel'))
    cancel.pack(side='left', padx=4)
    entry.bind('<Return>', send_text)

    def request(action, value=''):
        payload = None if action == 'status' else json.dumps({'text': value}).encode('utf-8')
        req = Request(base + action, data=payload, headers={'Content-Type': 'application/json'},
                      method='GET' if action == 'status' else 'POST')
        try:
            with urlopen(req, timeout=3) as response:
                return json.loads(response.read(30000).decode('utf-8'))
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(5000).decode('utf-8')).get('error', str(exc))
            except Exception:
                message = str(exc)
            raise RuntimeError(message) from None

    def worker():
        while not closed.is_set():
            try:
                action, value = tasks.get(timeout=.4)
            except queue.Empty:
                action, value = 'status', ''
            try:
                events.put(request(action, value))
            except Exception as exc:
                events.put({'state': 'offline', 'busy': False, 'error': str(exc)})

    animating = False
    def refresh():
        nonlocal animating
        if closed.is_set():
            return
        data = None
        while True:
            try:
                data = events.get_nowait()
            except queue.Empty:
                break
        if data is not None:
            state = data.get('state')
            busy = bool(data.get('busy'))
            label = data.get('message', '연결 확인 중')
            if state == 'recording':
                label = f"지금 말하시오 · 남은 {data.get('remaining', 0):.1f}초"
            elif state == 'analyzing':
                label = 'api 전달중'
            elif state == 'offline':
                label = '명령 서비스 연결 실패: ' + data.get('error', '')
            elif data.get('error'):
                label = data['error']
            if data.get('api_key_configured') is False:
                label = '로봇의 PiCommandConfig.py에 Claude API 키를 입력하세요.'
            status.set(label)
            if data.get('text'):
                detail = data['text']
                item = data.get('command')
                if item:
                    detail += ' → ' + item.get('reason', '')
                text.set(detail)
            send.configure(state='disabled' if busy else 'normal')
            voice.configure(state='disabled' if busy else 'normal')
            if busy and not animating:
                progress.start(12)
                animating = True
            elif not busy and animating:
                progress.stop()
                animating = False
        window.after(100, refresh)

    def destroy(event):
        if event.widget is window:
            closed.set()
    window.bind('<Destroy>', destroy, add='+')
    threading.Thread(target=worker, daemon=True, name='PiCommandPanel').start()
    window.after(100, refresh)
    return frame
