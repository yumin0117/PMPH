#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply smooth neck and gripper controls to the CURRENT PiCar-Pro files.

This patches existing files in-place only after validating them. Exact original
files are backed up together in Server/smooth_backups_<timestamp>.
Run from the Pi: python3 apply_smooth_integration.py --apply

Requires SmoothServo.py in the same directory as GUIServer_mobile.py.
Does NOT touch Move.py, RPIservo.py, OLED, YOLO, models or system packages.
"""

import argparse
import ast
from datetime import datetime
from pathlib import Path
import re
import shutil
import sys

SERVER_DIR = Path('/home/pi/Adeept_PiCar-Pro/Server')
PC_GUI_DIR = Path('/home/pi/move')
MARKER = 'SMOOTH_AB_INTEGRATION_v1'

NECK_MOBILE_CSS = r'''
/* SMOOTH_AB_INTEGRATION_v1 : camera neck control */
.neck-control-card .neck-title {font-size:18px;font-weight:700;margin:0 0 8px;}
.neck-control-card .neck-note {font-size:12px;opacity:.75;line-height:1.5;margin-bottom:12px;}
.neck-control-card .neck-joystick {position:relative;width:min(62vw,240px);height:min(62vw,240px);border:4px solid #bbc1c9;border-radius:50%;background:#51565e;margin:16px auto;touch-action:none;}
.neck-control-card .neck-joystick::before,.neck-control-card .neck-joystick::after {content:"";position:absolute;left:50%;top:50%;background:#ffffff70;transform:translate(-50%,-50%);pointer-events:none;}
.neck-control-card .neck-joystick::before{width:2px;height:100%;}
.neck-control-card .neck-joystick::after{width:100%;height:2px;}
.neck-control-card .neck-knob{position:absolute;left:50%;top:50%;width:34%;height:34%;border-radius:50%;background:#b7bec7;transform:translate(-50%,-50%);pointer-events:none;box-shadow:0 3px 10px #0004;}
.neck-control-card .neck-b-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px;}
.neck-control-card .neck-b-btn{border:1px solid #ccd1d8;border-radius:12px;background:#f4f6f8;color:#243142;font-weight:700;padding:17px 12px;touch-action:none;font-size:16px;}
.neck-control-card .neck-b-btn.active{background:#3182f6;color:#fff;}
'''

NECK_MOBILE_HTML = r'''
<!-- SMOOTH_AB_INTEGRATION_v1 -->
<div class="card neck-control-card">
    <div class="neck-title">A · 카메라 목</div>
    <div class="neck-note">상·하·좌·우 · 누르는 동안 부드럽게 회전 · 손을 떼면 그 위치에서 정지</div>
    <div class="neck-joystick" id="neckJoystick">
        <div class="neck-knob" id="neckKnob"></div>
    </div>
    <div class="neck-title">B · 목 하단 관절</div>
    <div class="neck-note">상·하 버튼으로 조작</div>
    <div class="neck-b-row">
        <button type="button" class="neck-b-btn" data-neck-axis="b_tilt" data-neck-dir="1">▲ 위</button>
        <button type="button" class="neck-b-btn" data-neck-axis="b_tilt" data-neck-dir="-1">▼ 아래</button>
    </div>
    <div class="neck-note">최초 테스트와 동일하게 중심 기준 ±12° 이내로 제한됩니다.</div>
</div>
'''

NECK_MOBILE_JS = r'''
/* SMOOTH_AB_INTEGRATION_v1 : neck joystick/button keepalive */
(function () {
    const j = document.getElementById('neckJoystick');
    if (!j) return;
    const k = document.getElementById('neckKnob');
    let seq = Date.now() * 1000;
    let heldPointer = null;
    let timer = null;
    let pan = 0;
    let tilt = 0;
    const bHolds = new Map();

    function neckSend(axis, value) {
        ++seq;
        fetch('/api/command', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command:'neck:' + axis + ':' + value, seq:seq}),
            cache: 'no-store', keepalive: true
        }).catch(() => {});
    }
    function readJoystick(event) {
        const r = j.getBoundingClientRect();
        const centerX = r.left + r.width / 2;
        const centerY = r.top + r.height / 2;
        const maxR = r.width * .31;
        let dx = event.clientX - centerX;
        let dy = event.clientY - centerY;
        const distance = Math.hypot(dx, dy);
        if (distance > maxR) { dx *= maxR / distance; dy *= maxR / distance; }
        k.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
        const x = dx / maxR, y = dy / maxR;
        const newPan = x > .25 ? 1 : x < -.25 ? -1 : 0;
        const newTilt = y < -.25 ? 1 : y > .25 ? -1 : 0;
        if (newPan !== pan) { pan = newPan; neckSend('a_pan', pan); }
        if (newTilt !== tilt) { tilt = newTilt; neckSend('a_tilt', tilt); }
    }
    function heartbeat() {
        if (heldPointer === null) return;
        if (pan) neckSend('a_pan', pan);
        if (tilt) neckSend('a_tilt', tilt);
    }
    function joystickStop() {
        if (timer !== null) clearInterval(timer);
        timer = null; heldPointer = null;
        pan = 0; tilt = 0;
        neckSend('a_pan', 0);
        neckSend('a_tilt', 0);
        k.style.transform = 'translate(-50%, -50%)';
    }
    j.addEventListener('pointerdown', e => {
        e.preventDefault();
        if (heldPointer !== null) return;
        heldPointer = e.pointerId;
        try { j.setPointerCapture(e.pointerId); } catch (_) {}
        readJoystick(e);
        timer = setInterval(heartbeat, 260);
    });
    j.addEventListener('pointermove', e => {
        if (heldPointer === e.pointerId) { e.preventDefault(); readJoystick(e); }
    });
    for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        j.addEventListener(event, () => { if (heldPointer !== null) joystickStop(); });
    }

    document.querySelectorAll('[data-neck-axis]').forEach(btn => {
        const axis = btn.dataset.neckAxis;
        const direction = Number(btn.dataset.neckDir);
        let id = null;
        let pointer = null;
        function endHold() {
            if (id !== null) clearInterval(id);
            id = null; pointer = null;
            bHolds.delete(btn);
            btn.classList.remove('active');
            neckSend(axis, 0);
        }
        btn.addEventListener('pointerdown', e => {
            e.preventDefault();
            if (id !== null) return;
            pointer = e.pointerId;
            btn.classList.add('active');
            try { btn.setPointerCapture(pointer); } catch (_) {}
            neckSend(axis, direction);
            id = setInterval(() => neckSend(axis, direction), 260);
            bHolds.set(btn, endHold);
        });
        for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) {
            btn.addEventListener(event, () => { if (id !== null) endHold(); });
        }
    });

    function stopAll() {
        if (heldPointer !== null) joystickStop();
        for (const stop of Array.from(bHolds.values())) stop();
    }
    window.addEventListener('blur', stopAll);
    window.addEventListener('pagehide', stopAll);
    document.addEventListener('visibilitychange', () => { if (document.hidden) stopAll(); });
})();
'''

# PC neck controls live in a separate small window so the existing camera,
# movement, OLED and gripper layout will not be squeezed or clipped.
NECK_PC_GUI = r'''
# SMOOTH_AB_INTEGRATION_v1
_neck_dialog = None
_neck_held = {}


def _neck_send(axis, direction):
    command_queue.put(f"neck:{axis}:{direction}")
    control_event.set()


def _neck_hold(btn, axis, direction):
    _neck_release(axis)
    _neck_send(axis, direction)
    btn.configure(bg=BLUE, fg=WHITE)

    def tick():
        state = _neck_held.get(axis)
        if state is None or state[0] is not btn:
            return
        _neck_send(axis, direction)
        state[1] = window.after(250, tick)

    job = window.after(250, tick)
    _neck_held[axis] = [btn, job]


def _neck_release(axis):
    state = _neck_held.pop(axis, None)
    if state is not None:
        btn, job = state
        window.after_cancel(job)
        try:
            btn.configure(bg=WHITE, fg=TEXT)
        except tk.TclError:
            pass
    _neck_send(axis, 0)


def open_neck_controls():
    global _neck_dialog
    if _neck_dialog is not None and _neck_dialog.winfo_exists():
        _neck_dialog.lift()
        return
    panel = tk.Toplevel(window)
    _neck_dialog = panel
    panel.title("A 카메라 목 / B 목 하단 관절")
    panel.configure(bg=WHITE)
    panel.geometry("420x460")
    panel.resizable(False, False)
    panel.transient(window)

    def make_button(parent, title, axis, direction, row, col):
        btn = tk.Button(parent, text=title, width=13, height=2,
                        bg=WHITE, fg=TEXT, relief='solid', bd=1,
                        font=button_font)
        btn.grid(row=row, column=col, padx=4, pady=4)
        btn.bind('<ButtonPress-1>', lambda e: _neck_hold(btn, axis, direction))
        btn.bind('<ButtonRelease-1>', lambda e: _neck_release(axis))
        btn.bind('<Leave>', lambda e: _neck_release(axis))
        return btn

    tk.Label(panel, text="A · 카메라 목", bg=WHITE, fg=TEXT,
             font=section_font).pack(pady=(15, 3))
    tk.Label(panel, text="상하좌우 · 버튼을 누르는 동안 이동", bg=WHITE,
             fg=SUBTEXT).pack()
    frame_a = tk.Frame(panel, bg=WHITE)
    frame_a.pack(pady=7)
    make_button(frame_a, "▲ 위", "a_tilt", 1, 0, 1)
    make_button(frame_a, "◀ 왼쪽", "a_pan", -1, 1, 0)
    make_button(frame_a, "오른쪽 ▶", "a_pan", 1, 1, 2)
    make_button(frame_a, "▼ 아래", "a_tilt", -1, 2, 1)

    tk.Label(panel, text="B · 목 하단 관절", bg=WHITE, fg=TEXT,
             font=section_font).pack(pady=(8, 3))
    frame_b = tk.Frame(panel, bg=WHITE)
    frame_b.pack(pady=7)
    make_button(frame_b, "▲ 위", "b_tilt", 1, 0, 0)
    make_button(frame_b, "▼ 아래", "b_tilt", -1, 0, 1)
    tk.Label(panel, text="각 축은 최초 기준 위치에서 ±12°만 이동합니다.",
             bg=WHITE, fg=SUBTEXT).pack(pady=4)

    def dismiss():
        for axis in ('a_pan', 'a_tilt', 'b_tilt'):
            _neck_release(axis)
        panel.destroy()
    panel.protocol('WM_DELETE_WINDOW', dismiss)

# End SMOOTH_AB_INTEGRATION_v1
'''

PC_GRIP_HEARTBEAT = r'''
# SMOOTH_AB_INTEGRATION_v1: gripper signal heartbeat (safe stop on disconnect)
_grip_beat_cmd = None
_grip_beat_job = None


def begin_grip_heartbeat(command):
    global _grip_beat_cmd, _grip_beat_job
    _grip_beat_cmd = command
    if _grip_beat_job is not None:
        return

    def tick():
        global _grip_beat_job
        _grip_beat_job = None
        if _grip_beat_cmd is not None and app_running:
            command_queue.put(_grip_beat_cmd)
            control_event.set()
            _grip_beat_job = window.after(320, tick)

    _grip_beat_job = window.after(320, tick)


def end_grip_heartbeat():
    global _grip_beat_cmd, _grip_beat_job
    _grip_beat_cmd = None
    if _grip_beat_job is not None:
        window.after_cancel(_grip_beat_job)
        _grip_beat_job = None
'''

MOBILE_GRIP_HEARTBEAT = r'''
/* SMOOTH_AB_INTEGRATION_v1: grip keepalive without servo restart */
(function () {
    let gripRepeat = null;
    document.querySelectorAll('.grip').forEach(btn => {
        btn.addEventListener('pointerdown', () => {
            if (gripRepeat !== null) clearInterval(gripRepeat);
            gripRepeat = setInterval(() => {
                if (gripButton === btn) queueGripCommand(btn.dataset.command);
            }, 320);
        });
        function end() {
            if (gripRepeat !== null) clearInterval(gripRepeat);
            gripRepeat = null;
        }
        btn.addEventListener('pointerup', end);
        btn.addEventListener('pointercancel', end);
        btn.addEventListener('lostpointercapture', end);
        btn.addEventListener('pointerleave', end);
        window.addEventListener('blur', end);
        document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
    });
})();
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def insert_after_first(text, needle, content):
    ix = text.find(needle)
    require(ix >= 0, f'패치 위치를 찾지 못했습니다: {needle[:60]!r}')
    return text[:ix + len(needle)] + content + text[ix + len(needle):]


def replace_first_regex(text, pattern, replacement, purpose):
    result, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    require(n == 1, f'{purpose} 수정 위치를 확인할 수 없습니다.')
    return result


def replace_top_function(text, name, code):
    """Use AST line boundaries, ensuring only the intended function changes."""
    parsed = ast.parse(text)
    funs = [node for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    require(len(funs) == 1, f'기존 {name} 함수가 하나 있어야 합니다.')
    lines = text.splitlines(keepends=True)
    node = funs[0]
    start = node.lineno - 1
    end = node.end_lineno
    result = ''.join(lines[:start]) + code.rstrip() + '\n' + ''.join(lines[end:])
    ast.parse(result)
    return result


def add_to_function_end(text, func_name, snippet):
    parsed = ast.parse(text)
    matching = [node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == func_name]
    require(len(matching) == 1, func_name + ' 함수가 없습니다')
    node = matching[0]
    lines = text.splitlines(keepends=True)
    idx = node.end_lineno
    lines.insert(idx, '    ' + snippet + '\n')
    result = ''.join(lines)
    ast.parse(result)
    return result


def inject_function_start(text, func_name, snippet):
    parsed = ast.parse(text)
    matching = [node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == func_name]
    require(len(matching) == 1, func_name + ' 함수가 없습니다')
    node = matching[0]
    lines = text.splitlines(keepends=True)
    idx = node.body[0].lineno - 1
    lines.insert(idx, snippet.rstrip() + '\n')
    result = ''.join(lines)
    ast.parse(result)
    return result


def inject_main_after_call(text, callee, snippet):
    parsed = ast.parse(text)
    main = next((n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
    require(main is not None, 'main 함수가 없습니다')
    items=[]
    for n in ast.walk(main):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.value and callee in (n.func.attr,):
            items.append(n)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee:
            items.append(n)
    require(len(items) >= 1, f'main에서 {callee} 호출을 찾을 수 없습니다')
    n=items[0]
    # Last line of the complete enclosing statement for multi-line calls.
    # Caller passes single-line initialize_servos()/move.setup().
    lines = text.splitlines(keepends=True)
    lines.insert(n.end_lineno, snippet.rstrip() + '\n')
    result = ''.join(lines)
    ast.parse(result)
    return result


def common_server(text):
    require(MARKER not in text, '이미 통합된 파일입니다. 중복 적용하지 마세요.')
    require('import RPIservo' in text, 'RPIservo import를 찾지 못했습니다.')
    text=insert_after_first(text,'import RPIservo\n', 'from SmoothServo import SmoothNeck, SmoothGripper  # '+MARKER+'\n')
    text=insert_after_first(text,'from SmoothServo import SmoothNeck, SmoothGripper  # '+MARKER+'\n', '\nneck_controller = None\ngrip_controller = None\n')
    text=replace_top_function(text,'gripper_start', '''def gripper_start(direction):
    if grip_controller is None:
        raise RuntimeError("집게 제어기 초기화 전입니다")
    grip_controller.hold(direction)
''')
    text=replace_top_function(text,'gripper_stop','''def gripper_stop():
    if grip_controller is not None:
        grip_controller.release()
''')
    text=inject_function_start(text, 'robot_ctrl', '''    if command.startswith("neck:"):
        if neck_controller is None:
            raise RuntimeError("목 제어기 초기화 전입니다")
        neck_controller.command(command)
        return
''')
    return text


def mobile(text):
    text = common_server(text)
    # Mobile HTTP supports sequence ordering even with parallel POST requests.
    text = replace_first_regex(text, r'^def robot_ctrl\(command\):', 'def robot_ctrl(command, neck_seq=None):', '모바일 robot_ctrl 인수')
    text = text.replace('neck_controller.command(command)', 'neck_controller.command(command, neck_seq)',1)
    # Change POST call, not the function definition.
    text, count = re.subn(r'(?<!def )robot_ctrl\(\s*command\s*\)', 'robot_ctrl(command, data.get("seq"))', text, count=1)
    require(count == 1, '모바일 POST의 robot_ctrl(command)를 찾지 못했습니다.')
    # The older mobile server forced channel1 to 90°; this must not override A joystick.
    text, fixed_count = re.subn(r'^\s*camera_servo\.set_angle\(\s*1\s*,\s*CAMERA_FIXED_ANGLE\s*\)\s*$', '\n    # 카메라 목 고정 해제: 초기 위치로 점프하지 않음', text, count=1,flags=re.MULTILINE)
    if fixed_count:
        text=text.replace('"CAMERA SERVO : "','"CAMERA SERVO : "')
        text=text.replace('f"{CAMERA_FIXED_ANGLE}도 고정"','"A/B 목 관절 제어"')
    text=inject_main_after_call(text,'initialize_servos','''    global neck_controller, grip_controller
    neck_controller = SmoothNeck()
    neck_controller.start()
    grip_controller = SmoothGripper(gripper_servo, speed=GRIPPER_SPEED)''')
    # neck safe stop at normal shutdown, do not immediately recenter.
    # Target the first gripper_stop() statement INSIDE main(), not its definition.
    astmod=ast.parse(text)
    main=next(n for n in astmod.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    stops=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='gripper_stop']
    require(stops,'mobile main() cleanup gripper_stop() missing')
    stop=stops[0]
    lines=text.splitlines(keepends=True)
    indent=' ' * stop.col_offset
    lines.insert(stop.lineno-1, f'{indent}if neck_controller is not None:\n{indent}    neck_controller.close()\n{indent}if grip_controller is not None:\n{indent}    grip_controller.close()\n')
    text=''.join(lines)
    # Add controls inside existing MOBILE_HTML (do not change YOLO or APIs).
    require('MOBILE_HTML' in text, 'MOBILE_HTML 문자열을 찾지 못했습니다')
    html_start=text.index('MOBILE_HTML')
    footer=re.search(r'<div\s+class\s*=\s*["\']footer["\']',text[html_start:])
    require(footer, '모바일 HTML footer 영역을 찾지 못했습니다')
    insert_at=html_start+footer.start()
    text=text[:insert_at]+NECK_MOBILE_HTML+'\n'+text[insert_at:]
    # Add style inside the existing HTML <style> for proper rendering.
    style_close=text.find('</style>',html_start)
    require(style_close >= 0, '모바일 CSS </style>를 찾지 못했습니다')
    text=text[:style_close]+NECK_MOBILE_CSS+'\n'+text[style_close:]
    script_close=text.find('</script>',html_start)
    require(script_close >= 0, '모바일 JS </script>를 찾지 못했습니다')
    text=text[:script_close]+NECK_MOBILE_JS+'\n'+MOBILE_GRIP_HEARTBEAT+'\n'+text[script_close:]
    ast.parse(text)
    return text


def pc_server(text):
    text=common_server(text)
    # Previously unused arm/hand commands are re-routed to the same smoother,
    # preventing concurrent RPIservo wiggle threads on channels 2 / 3.
    legacy=re.compile(r'^    elif command == "armup":.*?(?=^    elif command == "grab":)',re.MULTILINE|re.DOTALL)
    legacy_replacement='''    elif command == "armup":
        neck_controller.command("neck:b_tilt:1")
    elif command == "armdown":
        neck_controller.command("neck:b_tilt:-1")
    elif command == "armstop":
        neck_controller.command("neck:b_tilt:0")
    elif command == "handup":
        neck_controller.command("neck:a_tilt:1")
    elif command == "handdown":
        neck_controller.command("neck:a_tilt:-1")
    elif command == "HAstop":
        neck_controller.command("neck:a_tilt:0")

'''
    text,count=legacy.subn(legacy_replacement,text,count=1)
    require(count==1, 'PC arm/hand legacy mapping 영역을 찾지 못했습니다')
    text=inject_main_after_call(text,'setup','''    global neck_controller, grip_controller
    neck_controller = SmoothNeck()
    neck_controller.start()
    grip_controller = SmoothGripper(gripper_servo, speed=GRIPPER_SPEED)''')
    astmod=ast.parse(text)
    main=next(n for n in astmod.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    stops=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='gripper_stop']
    require(stops,'PC main() cleanup gripper_stop missing')
    stop=stops[0]
    lines=text.splitlines(keepends=True)
    indent=' '*stop.col_offset
    lines.insert(stop.lineno-1, f'{indent}if neck_controller is not None:\n{indent}    neck_controller.close()\n{indent}if grip_controller is not None:\n{indent}    grip_controller.close()\n')
    text=''.join(lines)
    ast.parse(text)
    return text


def pc_gui(text):
    require(MARKER not in text,'PC GUI 이미 통합됨')
    # Add a heartbeat to existing Z/X callbacks.
    text=insert_after_first(text,'# =========================================================\n# 집게\n# =========================================================\n', '\n'+PC_GRIP_HEARTBEAT+'\n')
    text=add_to_function_end(text,'arm_grab_start','begin_grip_heartbeat("grab")')
    text=add_to_function_end(text,'arm_release_start','begin_grip_heartbeat("loose")')
    text=add_to_function_end(text,'arm_stop','end_grip_heartbeat()')
    # Add separate popup controls and topbar launcher.
    text=insert_after_first(text,'# =========================================================\n# Header\n# =========================================================\n', '\n'+NECK_PC_GUI+'\n')
    text=insert_after_first(text,'server_box.pack(side="right")\n','''
neck_open_btn = tk.Button(
    header, text="A/B 목 제어", command=open_neck_controls,
    bg=WHITE, fg=BLUE, font=button_font,
    relief="solid", bd=1, padx=12, pady=7
)
neck_open_btn.pack(side="right", padx=10)
''')
    # Stop held commands while the Tk window still exists.
    text=inject_function_start(text,'close_program','''    end_grip_heartbeat()
    for axis in ("a_pan", "a_tilt", "b_tilt"):
        _neck_release(axis)
''')
    ast.parse(text)
    return text


def apply(targets, destination):
    # Patch entirely in memory and refuse *any* write on failed validation.
    updated={}
    for path, function in targets:
        require(path.is_file(), f'파일이 없습니다: {path}')
        original=path.read_text(encoding='utf-8')
        ast.parse(original)
        output=function(original)
        compile(output,str(path),'exec')
        updated[path]=output
        print(f'[검증 OK] {path.name}: {len(original.splitlines())} → {len(output.splitlines())}줄')

    smooth=SERVER_DIR/'SmoothServo.py'
    require(smooth.is_file(), f'SmoothServo.py 파일이 필요합니다: {smooth}')
    compile(smooth.read_text(encoding='utf-8'),str(smooth),'exec')
    if not destination:
        print('[미리보기] 아직 실제 파일을 수정하지 않았습니다. 적용하려면 --apply 를 붙이세요.')
        return

    backup=SERVER_DIR/('smooth_backups_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    backup.mkdir(parents=True,exist_ok=False)
    for path in updated:
        shutil.copy2(path,backup/path.name)
    print('[원본 백업]', backup)
    for path,out in updated.items():
        temp=path.with_name(path.name+'.smooth_tmp')
        temp.write_text(out,encoding='utf-8')
        temp.replace(path)
        print('[적용 완료]',path)
    print('[완료] YOLO 모델/폴더, OLED_eyes.py, Move.py, RPIservo.py는 수정하지 않았습니다.')


def main():
    global SERVER_DIR
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',help='검증 후 원본 백업 및 실제 반영')
    parser.add_argument('--server-dir',type=Path,default=SERVER_DIR,help='테스트 용도: 서버 경로')
    parser.add_argument('--gui-dir',type=Path,default=PC_GUI_DIR,help='테스트 용도: GUI 경로')
    args=parser.parse_args()
    SERVER_DIR=args.server_dir
    jobs=[(SERVER_DIR/'GUIServer_mobile.py',mobile),
          (SERVER_DIR/'GUIServer_custom.py',pc_server),
          (args.gui_dir/'robot_gui.py',pc_gui)]
    try:
        apply(jobs,args.apply)
    except Exception as exc:
        print('[중단] 자동 통합 실패:',exc)
        print('다른 파일을 덮어쓰지 않았는지 백업 상태를 확인하세요.')
        sys.exit(1)


if __name__=='__main__':
    main()
