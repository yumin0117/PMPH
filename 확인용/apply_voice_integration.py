#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely integrate ReSpeaker voice capture into the CURRENT robot servers.

Run once from the copied update directory on Raspberry Pi:
  python3 apply_voice_integration.py              # preview, no writes
  python3 apply_voice_integration.py --apply      # full backup + apply
Existing camera/YOLO/motor/neck/gripper logic remains in place.
"""

import argparse
import ast
import re
import shutil
from datetime import datetime
from pathlib import Path

SERVER_DEFAULT = Path('/home/pi/Adeept_PiCar-Pro/Server')
GUI_DEFAULT = Path('/home/pi/move')
MARKER = 'VOICE_REPEAKER_1'

VOICE_CSS = r'''
/* VOICE_REPEAKER_1: Voice GUI */
.voice-card .voice-button {width:100%;border:0;border-radius:14px;padding:19px 12px;background:#3182F6;color:white;font-weight:750;font-size:17px;touch-action:none;user-select:none;-webkit-user-select:none;}
.voice-card .voice-button.recording {background:#D9534F;}
.voice-card .voice-text {margin-top:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.7;}
.voice-card .voice-hint {font-size:12px;opacity:.72;margin-top:9px;}
'''
VOICE_HTML = r'''
<!-- VOICE_REPEAKER_1 -->
<div class="card voice-card">
    <div class="card-title">🎤 한국어 음성 인식</div>
    <button type="button" id="piVoiceButton" class="voice-button">🎤 누르고 말하기</button>
    <div class="voice-hint">라즈베리파이에 연결한 ReSpeaker Lite로 녹음합니다.<br>누르고 있는 동안 녹음 · 놓으면 음성 인식</div>
    <div class="voice-text" id="piVoiceStatus">음성 인식 준비 상태 확인 중...</div>
    <div class="voice-text" id="piVoiceText"></div>
</div>
'''
VOICE_JS = r'''
/* VOICE_REPEAKER_1: use SAME ORIGIN proxy, never browser microphone permission */
(function () {
    const btn = document.getElementById('piVoiceButton');
    if (!btn) return;
    const status = document.getElementById('piVoiceStatus');
    const output = document.getElementById('piVoiceText');
    let held = false;
    let sequence = Promise.resolve();
    let lastResult = '';

    function voiceAction(action) {
        // Start and stop are serialized to avoid reversed requests on slow Wi-Fi.
        sequence = sequence.catch(() => {}).then(async () => {
            const response = await fetch('/api/voice/' + action, {
                method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'},
                body:'{}', keepalive:true
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
        }).catch(err => {
            status.textContent = '마이크 명령 전송 오류: ' + err.message;
        });
    }
    function start(e) {
        if (e) e.preventDefault();
        if (held) return;
        held = true;
        btn.classList.add('recording');
        btn.textContent = '● 녹음 중 · 손을 떼면 인식';
        voiceAction('start');
    }
    const voiceHeartbeat = setInterval(() => {
        if (held) voiceAction('ping');
    }, 650);
    function end() {
        if (!held) return;
        held = false;
        btn.classList.remove('recording');
        btn.textContent = '🎤 누르고 말하기';
        voiceAction('stop');
    }
    btn.addEventListener('pointerdown', e => {
        start(e);
        try { btn.setPointerCapture(e.pointerId); } catch (_) {}
    });
    for (const ev of ['pointerup','pointercancel','lostpointercapture']) {
        btn.addEventListener(ev, end);
    }
    window.addEventListener('blur', end);
    window.addEventListener('pagehide', end);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) end();
    });

    async function refreshVoice() {
        try {
            const response = await fetch('/api/voice/status', {cache:'no-store'});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const data = await response.json();
            const names = {
                loading:'음성 모델 준비 중', ready:'음성 인식 준비 완료',
                opening:'마이크 연결 중', recording:'녹음 중',
                processing:'한국어 인식 중', done:'음성 인식 완료',
                error:'음성 오류', offline:'음성 서비스 오프라인'
            };
            status.textContent = names[data.state] || data.message || '연결 확인 중';
            if (data.state === 'done' && data.text && data.text !== lastResult) {
                lastResult = data.text;
                output.textContent = '인식 결과: ' + data.text +
                    (data.elapsed != null ? ' (' + data.elapsed + '초)' : '');
            }
            if (data.state === 'error' || data.state === 'offline') {
                output.textContent = data.error || data.message || '마이크 연결 확인 필요';
            }
        } catch (err) {
            status.textContent = '음성 서비스 연결 확인 중';
        }
    }
    refreshVoice();
    setInterval(refreshVoice, 650);
})();
'''


def must(condition, message):
    if not condition:
        raise RuntimeError(message)


def after_imports(src, statement):
    tree = ast.parse(src)
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    must(imports, '기존 import 영역을 찾을 수 없습니다')
    lines = src.splitlines(keepends=True)
    lines.insert(max(x.end_lineno for x in imports), '\n' + statement + '\n')
    result = ''.join(lines)
    ast.parse(result)
    return result


def add_before_main_call(src):
    tree = ast.parse(src)
    entry = []
    for top in tree.body:
        if isinstance(top, ast.If) and isinstance(top.test, ast.Compare):
            if isinstance(top.test.left, ast.Name) and top.test.left.id == '__name__':
                entry.append(top)
    must(len(entry) == 1, 'if __name__ == "__main__" 영역을 확인할 수 없습니다')
    lines = src.splitlines(keepends=True)
    calls = [x for x in entry[0].body if isinstance(x, ast.Expr)
             and isinstance(x.value, ast.Call) and isinstance(x.value.func, ast.Name)
             and x.value.func.id == 'main']
    must(len(calls) == 1, '최종 main() 호출을 찾지 못했습니다')
    n = calls[0]
    lines.insert(n.lineno - 1, ' ' * n.col_offset + 'start_voice()  # ' + MARKER + '\n')
    output = ''.join(lines)
    ast.parse(output)
    return output


def insert_function_body(src, class_name, method, snippet):
    tree = ast.parse(src)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    must(len(classes) == 1, f'{class_name}를 찾지 못했습니다')
    functions = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == method]
    must(len(functions) == 1, f'{class_name}.{method}를 찾지 못했습니다')
    f = functions[0]
    lines = src.splitlines(keepends=True)
    indent = ' ' * (f.col_offset + 4)
    insert_at = f.body[0].lineno - 1
    if isinstance(f.body[0], ast.Expr) and isinstance(f.body[0].value, ast.Constant) and isinstance(f.body[0].value.value, str):
        insert_at = f.body[0].end_lineno
    text = ''.join(indent + l + '\n' for l in snippet.strip('\n').split('\n'))
    lines.insert(insert_at, text)
    result = ''.join(lines)
    ast.parse(result)
    return result


def mobile_update(src):
    must(MARKER not in src, '모바일 파일에 이미 음성 기능이 들어 있습니다')
    must('MOBILE_HTML' in src, '모바일 HTML 변수를 찾지 못했습니다')
    must('class MobileHandler' in src, 'MobileHandler 클래스가 없습니다')
    must('def send_json' in src, 'send_json()이 없습니다')
    src = after_imports(src, 'from PiVoiceLauncher import start_voice, voice_request')
    src = add_before_main_call(src)
    src = insert_function_body(src, 'MobileHandler', 'do_GET', '''if self.path.split('?', 1)[0] == '/api/voice/status':
    self.send_json(voice_request('status'))
    return''')
    src = insert_function_body(src, 'MobileHandler', 'do_POST', '''if self.path.split('?', 1)[0] in ('/api/voice/start', '/api/voice/stop', '/api/voice/ping'):
    origin = self.headers.get('Origin')
    local_origin = 'http://' + self.headers.get('Host', '')
    if origin and origin != local_origin:
        self.send_json({'state': 'error', 'error': '잘못된 접속 출처'}, status=403)
        return
    action = self.path.rsplit('/', 1)[-1]
    self.send_json(voice_request(action))
    return''')
    i = src.index('MOBILE_HTML')
    end_style = src.find('</style>', i)
    end_script = src.find('</script>', i)
    must(end_style >= 0 and end_script >= 0, '기존 모바일 CSS/JavaScript를 찾지 못했습니다')
    src = src[:end_style] + VOICE_CSS + '\n' + src[end_style:]
    i = src.index('MOBILE_HTML')
    footer = re.search(r'<div\s+class\s*=\s*["\']footer["\']', src[i:])
    must(footer is not None, '모바일 하단 footer 위치를 찾지 못했습니다')
    pos = i + footer.start()
    src = src[:pos] + VOICE_HTML + '\n' + src[pos:]
    i = src.index('MOBILE_HTML')
    end_script = src.find('</script>', i)
    src = src[:end_script] + VOICE_JS + '\n' + src[end_script:]
    compile(src, 'GUIServer_mobile.py', 'exec')
    return src


def pc_server_update(src):
    must(MARKER not in src, 'PC 서버에 이미 음성 기능이 들어 있습니다')
    src = after_imports(src, 'from PiVoiceLauncher import start_voice')
    src = add_before_main_call(src)
    compile(src, 'GUIServer_custom.py', 'exec')
    return src


def pc_gui_update(src):
    must(MARKER not in src, 'PC GUI에 이미 음성 기능이 들어 있습니다')
    must('server_status' in src and 'header_left' in src and 'window =' in src,
         'PC GUI 레이아웃을 확인할 수 없습니다')
    src = after_imports(src, 'from VoicePCGUI import attach_voice_panel')
    matches = list(re.finditer(r'server_status\.pack\(\s*side\s*=\s*["\']left["\']\s*\)', src))
    must(len(matches) == 1, 'PC GUI 상태 표시 부분을 찾지 못했습니다')
    pos = matches[0].end()
    src = src[:pos] + '\n\nattach_voice_panel(window, header, header_left)  # ' + MARKER + '\n' + src[pos:]
    compile(src, 'robot_gui.py', 'exec')
    return src


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--server-dir', type=Path, default=SERVER_DEFAULT)
    parser.add_argument('--gui-dir', type=Path, default=GUI_DEFAULT)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    server = args.server_dir
    gui = args.gui_dir
    targets = [
        (server / 'GUIServer_mobile.py', mobile_update),
        (server / 'GUIServer_custom.py', pc_server_update),
        (gui / 'robot_gui.py', pc_gui_update),
    ]
    updated = {}
    try:
        for filepath, transform in targets:
            must(filepath.is_file(), f'기존 파일 없음: {filepath}')
            original = filepath.read_text(encoding='utf-8')
            compile(original, str(filepath), 'exec')
            result = transform(original)
            updated[filepath] = result
            print(f'[검증 OK] {filepath}: {len(original.splitlines())}줄 → {len(result.splitlines())}줄')
        for name in ('PiVoice.py', 'PiVoiceLauncher.py', 'VoicePCGUI.py'):
            src = root / name
            must(src.is_file(), f'필요한 구성 파일 없음: {src}')
            compile(src.read_text(encoding='utf-8'), str(src), 'exec')
        if not args.apply:
            print('[미리보기 완료] 아직 수정한 파일은 없습니다. 적용: python3 apply_voice_integration.py --apply')
            return
        backup = server / ('voice_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
        backup.mkdir(parents=True, exist_ok=False)
        for path, contents in updated.items():
            shutil.copy2(path, backup / path.name)
        for name in ('PiVoice.py', 'PiVoiceLauncher.py'):
            dst = server / name
            if dst.exists():
                shutil.copy2(dst, backup / name)
        pc_helper = gui / 'VoicePCGUI.py'
        if pc_helper.exists():
            shutil.copy2(pc_helper, backup / pc_helper.name)
        for path, contents in updated.items():
            stage = path.with_name(path.name + '.voice_tmp')
            stage.write_text(contents, encoding='utf-8')
            stage.replace(path)
            print('[적용]', path)
        for filename, destination in (
            ('PiVoice.py', server), ('PiVoiceLauncher.py', server), ('VoicePCGUI.py', gui)
        ):
            shutil.copy2(root / filename, destination / filename)
            print('[추가]', destination / filename)
        print('[원본 백업]', backup)
        print('[완료] 이동·목·집게·OLED·YOLO 및 모델 파일은 직접 수정하지 않았습니다.')
    except Exception as e:
        print('[검증/적용 오류]', e)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
