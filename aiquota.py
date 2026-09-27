#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 쿼터 — Claude · Codex 사용량 / 잔여량 / 리셋 시각 + 리셋 추천
================================================================
  python aiquota.py --widget   작은 위젯 (항상 위, 드래그, 우클릭 메뉴)
  python aiquota.py --web      브라우저 대시보드 (http://127.0.0.1:8787)
  python aiquota.py --watch    터미널 라이브 모드
  python aiquota.py            한 번 보고 끝 (터미널)
  python aiquota.py --setup    Claude 연결 설정 (claude.ai 로그인 방식)
  python aiquota.py --line     한 줄 요약 (상태바·프롬프트용)
  python aiquota.py --json     원본 데이터(JSON)
  python aiquota.py --doctor   진단
  python aiquota.py --demo     가짜 데이터로 미리보기 (다른 옵션과 같이 사용)

외부 라이브러리 없이 Python 3.8+ 표준 라이브러리만 사용합니다.
토큰은 이 PC에서만 읽고, 각 회사 서버(api.anthropic.com / claude.ai / chatgpt.com)로만 보냅니다.
"""
import argparse
import base64
import hashlib
import ctypes
import hmac
import json
import math
import os
import re
import secrets
import shutil
import statistics
import subprocess
import tempfile
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CODEX_DIR = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def _default_data_dir():
    """[보안] Windows: %LOCALAPPDATA%\\AIQuota — 로밍 프로필·OneDrive 로 동기화되지 않는 위치."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "AIQuota"
    return None


DATA_DIR = _default_data_dir()
_FILES = {  # 환경변수, 새 이름, 예전(홈 폴더) 이름
    "cache": ("AIQUOTA_CACHE", "cache.json", ".aiquota_cache.json"),
    "config": ("AIQUOTA_CONFIG", "config.json", ".aiquota_config.json"),
    "history": ("AIQUOTA_HISTORY", "history.jsonl", ".aiquota_history.jsonl"),
}


def _data_path(kind):
    env, new, old = _FILES[kind]
    if os.environ.get(env):
        return Path(os.environ[env])
    return DATA_DIR / new if DATA_DIR else HOME / old


CACHE_FILE = _data_path("cache")
CONFIG_FILE = _data_path("config")
HISTORY_FILE = _data_path("history")


def migrate_legacy_files():
    """예전 버전이 홈 폴더에 만든 파일을 새 위치로 옮기고, 남은 옛 파일(암호화된 키 사본 포함)은 지움."""
    if not DATA_DIR:
        return
    for kind, (env, new, old) in _FILES.items():
        if os.environ.get(env):
            continue
        src, dst = HOME / old, DATA_DIR / new
        if not src.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
            src.unlink()
        except OSError:
            pass

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_WEB = "https://claude.ai"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_UA = os.environ.get("AIQUOTA_CLAUDE_UA", "claude-code/2.1.100")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Claude 사용량 조회는 너무 자주 부르면 429가 나므로 최소 간격을 둔다.
CLAUDE_MIN_INTERVAL = 180
CODEX_MIN_INTERVAL = 60
FORCE_MIN_INTERVAL = 45
LOCAL_SCAN_TTL = 90
PROFILE_TTL = 20 * 60

WEEKDAYS = "월화수목금토일"
DEMO = False

CLAUDE_CONNECT_HINT = "Claude 연결 필요 — 위젯 우클릭 ▸ Claude 연결, 또는 python aiquota.py --setup"
SESSIONKEY_HELP = (
    "claude.ai 로그인 정보(sessionKey)로 Claude 한도를 읽어와요.\n\n"
    "1) 크롬/엣지에서 claude.ai 에 로그인\n"
    "2) F12 → Application(애플리케이션) → Cookies → https://claude.ai\n"
    "3) 이름이 sessionKey 인 값(sk-ant-sid… 로 시작)을 복사해 붙여넣기\n\n"
    "※ 이 값은 로그인 그 자체예요. 이 프로그램 말고 다른 곳·다른 사람에게는 절대 붙여넣지 마세요.\n"
    "※ 이 PC에만 저장되고 claude.ai 로만 전송돼요. 연결 끊기로 언제든 지울 수 있어요."
)


# ─────────────────────────────────────────────────────────────
#  공통 유틸
# ─────────────────────────────────────────────────────────────
def now_ts():
    return time.time()


def parse_ts(v):
    """epoch(초/밀리초) 또는 ISO-8601 문자열 → epoch 초."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return v / 1000.0 if v > 1e12 else v
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return parse_ts(float(s))
        s = s.replace("Z", "+00:00")
        m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
        if m:
            frac = (m.group(2) or "")[:7]
            if frac:
                frac = frac.ljust(7, "0")
            s = m.group(1) + frac + m.group(3)
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt.timestamp()
        except ValueError:
            return None
    return None


def local_dt(ts):
    return datetime.fromtimestamp(ts).astimezone()


def fmt_dur(sec, short=False, coarse=False):
    if sec is None:
        return "-"
    sec = int(max(0, sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if short:
        if d:
            return f"{d}d{h}h"
        if h:
            return f"{h}h{m:02d}m"
        return f"{m}m"
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m:02d}분"
    if m:
        return f"{m}분" if coarse else f"{m}분 {s:02d}초"
    return "1분 미만" if coarse else f"{s}초"


def fmt_when(ts, now=None):
    now = now or now_ts()
    dt = local_dt(ts)
    delta = (dt.date() - local_dt(now).date()).days
    if delta == 0:
        day = "오늘"
    elif delta == 1:
        day = "내일"
    elif delta == 2:
        day = "모레"
    else:
        day = f"{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]})"
    return f"{day} {dt:%H:%M}"


def fmt_when_short(ts, now=None):
    now = now or now_ts()
    dt = local_dt(ts)
    delta = (dt.date() - local_dt(now).date()).days
    if delta == 0:
        day = "오늘"
    elif delta == 1:
        day = "내일"
    elif 0 < delta < 7:
        day = WEEKDAYS[dt.weekday()]
    else:
        day = f"{dt.month}/{dt.day}"
    return f"{day} {dt:%H:%M}"


def day_hour(ts, now=None):
    """'화요일 10시' 처럼 쉬운 말 (분은 30분 단위 반올림)."""
    now = now or now_ts()
    dt = local_dt(ts + 15 * 60)
    dt = dt.replace(minute=0 if dt.minute < 30 else 30)
    delta = (dt.date() - local_dt(now).date()).days
    day = "오늘" if delta == 0 else "내일" if delta == 1 else f"{WEEKDAYS[dt.weekday()]}요일"
    return f"{day} {dt.hour}시" + (" 반" if dt.minute else "")


def hm(x):
    """시(float) → 'HH:MM'"""
    x = x % 24
    h = int(x)
    m = int(round((x - h) * 60))
    if m == 60:
        h, m = (h + 1) % 24, 0
    return f"{h:02d}:{m:02d}"


def fmt_tokens(n):
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            v = n / div
            return (f"{v:.1f}" if v < 100 else f"{v:.0f}") + unit
    return f"{int(n)}"


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path, data):
    """[보안] 소유자만 읽을 수 있는(0600) 임시 파일에 쓴 뒤 원자적으로 교체."""
    tmp = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".aiquota-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def append_private(path, line):
    """[보안] 0600 권한으로 한 줄 추가."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line)


_cache_lock = threading.Lock()


def load_cache():
    return read_json(CACHE_FILE) or {}


def save_cache_part(key, value):
    with _cache_lock:
        c = load_cache()
        if value is None:
            c.pop(key, None)
        else:
            c[key] = value
        write_json_atomic(CACHE_FILE, c)


# ─── 설정 파일 + 비밀값 암호화(Windows DPAPI) ─────────────────
_cfg_lock = threading.Lock()


def load_config():
    return read_json(CONFIG_FILE) or {}


def update_config(**kv):
    with _cfg_lock:
        c = load_config()
        for k, v in kv.items():
            if v is None:
                c.pop(k, None)
            else:
                c[k] = v
        write_json_atomic(CONFIG_FILE, c)
        return c


_ENTROPY = b"AIQuota/claude-session-key/v2"


def _use_dpapi():
    return os.name == "nt"


def _dpapi(data, protect, entropy=None):
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    blob_in, _k1 = blob(data)
    ent, _k2 = blob(entropy) if entropy else (None, None)
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    # dwFlags=1: CRYPTPROTECT_UI_FORBIDDEN
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(ent) if ent else None, None, None, 1, ctypes.byref(blob_out)):
        raise OSError("DPAPI 실패")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


SESSIONKEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{16,400}$")
ORG_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_KEY_IN_TEXT = re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,400}")


def normalize_session_key(key):
    """붙여넣은 값 정리 + 형식 검증. 이상하면 None."""
    key = (key or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("sessionkey="):
        key = key.split("=", 1)[1].split(";", 1)[0].strip()
    return key if SESSIONKEY_RE.match(key) else None


def secret_storage_label():
    return "Windows 계정으로 암호화(DPAPI)" if _use_dpapi() else "암호화 없이 소유자 전용(600) 파일"


def set_secret(name, value):
    if not value:
        update_config(**{name: None})
        return
    raw = value.encode("utf-8")
    if _use_dpapi():
        enc = "dpapi2:" + base64.b64encode(_dpapi(raw, True, _ENTROPY)).decode()  # 실패하면 평문 저장 없이 오류
        update_config(**{name: enc})
        return
    update_config(**{name: "plain:" + base64.b64encode(raw).decode()})


def get_secret(name):
    if name == "claude_session_key" and os.environ.get("AIQUOTA_CLAUDE_SESSION_KEY"):
        return normalize_session_key(os.environ["AIQUOTA_CLAUDE_SESSION_KEY"])
    v = load_config().get(name)
    if not v or not isinstance(v, str):
        return None
    try:
        kind, _, data = v.partition(":")
        raw = base64.b64decode(data)
        if kind == "dpapi2" and _use_dpapi():
            return _dpapi(raw, False, _ENTROPY).decode("utf-8")
        if kind == "dpapi" and _use_dpapi():          # 예전 형식 → 엔트로피 적용 형식으로 자동 이전
            val = _dpapi(raw, False).decode("utf-8")
            try:
                set_secret(name, val)
            except Exception:
                pass
            return val
        if kind == "plain" and not _use_dpapi():
            return raw.decode("utf-8")
        return None
    except Exception:
        return None


# ─── 클립보드 정리: 붙여넣은 sessionKey 가 클립보드·클립보드 기록(Win+V)에 남지 않게 ───
_PS_HISTORY = r"""
$ErrorActionPreference = 'Stop'
$target = '__TARGET__'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $null = $task.Wait(8000); $task.Result }
  $null = [Windows.ApplicationModel.DataTransfer.Clipboard, Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]
  $sha = [System.Security.Cryptography.SHA256]::Create()
  function H([string]$v) { ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($v)) | ForEach-Object { $_.ToString('x2') }) -join '' }
  $res = Await ([Windows.ApplicationModel.DataTransfer.Clipboard]::GetHistoryItemsAsync()) ([Windows.ApplicationModel.DataTransfer.ClipboardHistoryItemsResult])
  if ($res.Status -ne 'Success') { Write-Output ('STATUS ' + $res.Status); exit }
  $n = 0
  foreach ($it in @($res.Items)) {
    try {
      if (-not $it.Content.Contains('Text')) { continue }
      $txt = Await ($it.Content.GetTextAsync()) ([string])
      foreach ($m in [regex]::Matches($txt, 'sk-ant-[A-Za-z0-9_\-]{16,400}')) {
        if ((H $m.Value) -eq $target) { $null = [Windows.ApplicationModel.DataTransfer.Clipboard]::DeleteItemFromHistory($it); $n++; break }
      }
    } catch {}
  }
  Write-Output ('OK ' + $n)
} catch { Write-Output 'FAIL' }
"""


def clipboard_history_script(key):
    """[보안] 스크립트에는 키가 아니라 SHA-256 해시만 들어감 → PowerShell 로그·프로세스 목록에 키가 안 남음."""
    return _PS_HISTORY.replace("__TARGET__", hashlib.sha256(key.encode("utf-8")).hexdigest())


def key_in_text(key, text):
    return bool(key and text) and any(m == key for m in _KEY_IN_TEXT.findall(str(text)))


def _win_clipboard(clear_if=None):
    """현재 클립보드 텍스트 읽기. clear_if 키가 들어 있으면 비움 → 비웠으면 True."""
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [wintypes.UINT]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        h = u.GetClipboardData(13)  # CF_UNICODETEXT
        text = ""
        if h:
            p = k.GlobalLock(h)
            if p:
                try:
                    text = ctypes.wstring_at(p)
                finally:
                    k.GlobalUnlock(h)
        if clear_if and key_in_text(clear_if, text):
            u.EmptyClipboard()
            return True
        return False
    finally:
        u.CloseClipboard()


def scrub_clipboard(key, tk_root=None):
    """붙여넣은 키를 클립보드와 클립보드 기록에서 지움.
    반환: {"current": True/False/None, "history": 지운 개수 | "off" | None(못 지움)}"""
    res = {"current": None, "history": None}
    try:
        if os.name == "nt":
            res["current"] = _win_clipboard(clear_if=key)
        elif tk_root is not None:
            try:
                cur = tk_root.clipboard_get()
            except Exception:
                cur = ""
            if key_in_text(key, cur):
                tk_root.clipboard_clear()
                tk_root.clipboard_append(" ")
                res["current"] = True
            else:
                res["current"] = False
        elif sys.platform == "darwin":
            cur = subprocess.run(["pbpaste"], capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
            if key_in_text(key, cur):
                subprocess.run(["pbcopy"], input=b"", timeout=5)
                res["current"] = True
            else:
                res["current"] = False
    except Exception:
        pass
    if os.name == "nt":
        try:
            ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            enc = base64.b64encode(clipboard_history_script(key).encode("utf-16-le")).decode()
            p = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-EncodedCommand", enc], capture_output=True, timeout=40, creationflags=0x08000000)
            out = p.stdout.decode("utf-8", "replace")
            m = re.search(r"OK (\d+)", out)
            if m:
                res["history"] = int(m.group(1))
            elif "ClipboardHistoryDisabled" in out:
                res["history"] = "off"
        except Exception:
            pass
    return res


def scrub_report(res):
    """사용자에게 보여줄 정리 결과 문구 (키 내용은 절대 포함하지 않음)."""
    lines = []
    if res.get("current"):
        lines.append("✔ 클립보드에서 지웠어요")
    h = res.get("history")
    if os.name == "nt":
        if h == "off":
            lines.append("✔ 클립보드 기록(Win+V)이 꺼져 있어 남은 게 없어요")
        elif isinstance(h, int):
            lines.append(f"✔ 클립보드 기록(Win+V)에서도 지웠어요 ({h}개)" if h else "✔ 클립보드 기록(Win+V)에 남은 게 없어요")
        else:
            lines.append("⚠ 클립보드 기록은 자동으로 못 지웠어요 → Win+V 를 눌러 sk-ant- 로 시작하는 항목의 [⋯] ▸ 삭제")
    return "\n".join(lines)


# ─── HTTP ─────────────────────────────────────────────────────
class CloudflareBlocked(RuntimeError):
    pass


ALLOWED_HOSTS = {"api.anthropic.com", "claude.ai", "chatgpt.com"}
ALLOWED_SCHEMES = {"https"}
MAX_BODY = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """[보안] 리다이렉트를 따라가지 않음 — 따라가면 인증 헤더/쿠키가 다른 호스트로 전달될 수 있음."""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "리다이렉트 차단", hdrs, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_url(url):
    u = urlparse(url)
    if u.scheme not in ALLOWED_SCHEMES or (u.hostname or "") not in ALLOWED_HOSTS or u.username or u.password:
        raise RuntimeError("허용되지 않은 주소로의 요청을 막았어요")


def http_get_json(url, headers, timeout=12):
    check_url(url)
    req = urllib.request.Request(url, headers=headers, method="GET")
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise RuntimeError("응답이 너무 커요")
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError("응답이 JSON이 아니에요 (로그인 페이지로 튕겼을 수 있음)")


def curl_get_json(url, headers, timeout=15):
    check_url(url)
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        raise CloudflareBlocked()
    # [보안] 헤더(쿠키 포함)는 명령줄 인자가 아니라 stdin 으로 전달 → 작업 관리자/ps 에 비밀값이 안 보임
    cmd = [exe, "-sS", "--compressed", "-m", str(timeout), "--max-filesize", str(MAX_BODY),
           "--max-redirs", "0", "-H", "@-", "-w", "\n__HTTP__%{http_code}"]
    if "https" in ALLOWED_SCHEMES and len(ALLOWED_SCHEMES) == 1:
        cmd += ["--proto", "=https"]
    cmd.append(url)
    hdr = "".join(f"{k}: {v}\n" for k, v in headers.items() if "\n" not in f"{k}{v}" and "\r" not in f"{k}{v}")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    p = subprocess.run(cmd, input=hdr.encode("utf-8"), capture_output=True, timeout=timeout + 5, creationflags=flags)
    out = p.stdout.decode("utf-8", "replace")
    body, _, code = out.rpartition("\n__HTTP__")
    try:
        code = int(code.strip() or 0)
    except ValueError:
        code = 0
    low = body[:3000].lower()
    if code == 200:
        try:
            return json.loads(body)
        except ValueError:
            raise CloudflareBlocked()
    if code == 0:
        raise urllib.error.URLError(redact(p.stderr.decode("utf-8", "replace").strip()[:300]) or "curl 실패")
    if code in (403, 503) and ("cloudflare" in low or "just a moment" in low or "<html" in low):
        raise CloudflareBlocked()
    raise urllib.error.HTTPError(url, code, "curl", None, None)


def web_get_json(url, headers, timeout=15):
    """claude.ai 용: urllib가 보안 확인에 막히면 curl로 한 번 더."""
    try:
        return http_get_json(url, headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (403, 503):
            return curl_get_json(url, headers, timeout)
        raise
    except RuntimeError:
        return curl_get_json(url, headers, timeout)


_REDACT = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "***jwt***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]+"), "Bearer ***"),
    (re.compile(r"(?i)sessionKey=[^;\s]+"), "sessionKey=***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***@"),
]
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def redact(s):
    s = str(s)
    for rx, rp in _REDACT:
        s = rx.sub(rp, s)
    return s


def clean(s, limit=300):
    """[보안] 외부에서 온 글자에서 터미널 제어문자·양방향 제어문자 제거 + 길이 제한 + 비밀값 가리기."""
    if s is None:
        return None
    s = _CTRL.sub("", redact(s)).replace("\n", " ").replace("\t", " ")
    return s[:limit]


def http_error_text(e, who):
    return clean(_http_error_text(e, who))


def _http_error_text(e, who):
    if isinstance(e, CloudflareBlocked):
        return "claude.ai 보안 확인(Cloudflare)에 막혔어요 — 잠시 뒤 재시도, 또는 Claude Code CLI 로그인 방식 권장"
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return f"로그인 토큰 만료 — {who}를 한 번 실행하면 자동 갱신돼요"
        if e.code == 403:
            return "권한 없음 (구독 플랜이 아니거나 조직 계정일 수 있어요)"
        if e.code == 429:
            return "조회 요청이 너무 잦아 잠시 막힘 — 몇 분 뒤 자동 재시도"
        return f"서버 응답 {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"네트워크 연결 실패 ({getattr(e, 'reason', e)})"
    return str(e) or e.__class__.__name__


def tail_lines(path, max_bytes=4 * 1024 * 1024):
    """큰 로그 파일도 빠르게: 끝부분만 읽어서 줄 목록 반환."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def find_key(obj, key, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key, depth + 1)
            if r is not None:
                return r
    return None


# ─────────────────────────────────────────────────────────────
#  창(window) 정규화 · 분석
# ─────────────────────────────────────────────────────────────
def label_for_seconds(sec):
    if not sec:
        return "5h", "5시간 세션"
    if sec <= 6 * 3600:
        return "5h", "5시간 세션"
    if sec >= 6 * 86400:
        return "7d", "주간 한도"
    return f"{int(sec // 3600)}h", f"{int(sec // 3600)}시간 한도"


def make_window(key, label, used, resets_at, window_sec, primary=True):
    try:
        used = float(used)
    except (TypeError, ValueError):
        used = 0.0
    if not math.isfinite(used):        # [보안] NaN/Infinity 방어
        used = 0.0
    if resets_at is not None and (not isinstance(resets_at, (int, float)) or not math.isfinite(resets_at)):
        resets_at = None
    return {
        "key": key,
        "label": label,
        "used": max(0.0, min(100.0, used)),
        "resets_at": resets_at,
        "window_sec": window_sec,
        "primary": primary,
    }


def analyze_window(w, now):
    """잔여량, 페이스, 소진 예상 시각 등 계산."""
    w = dict(w)
    if w.get("reset_passed"):
        return w
    used = w["used"]
    ra = w.get("resets_at")
    sec = w.get("window_sec") or 0
    w["reset_passed"] = False
    if ra and ra <= now:
        w["reset_passed"] = True
        w["used"] = used = 0.0
        w["resets_at"] = ra = None
    w["remaining"] = round(100.0 - used, 1)
    w["seconds_left"] = (ra - now) if ra else None
    w["expected"] = None
    w["eta_empty"] = None
    w["projected"] = None

    if w["reset_passed"]:
        w["status"] = "fresh"
        w["status_text"] = "리셋 완료 · 다음 요청부터 새 창 시작"
        return w
    if used >= 99.5:
        w["status"] = "empty"
        w["status_text"] = f"한도 소진 · {fmt_when(ra, now)}에 풀려요" if ra else "한도 소진"
        return w
    if not ra or not sec:
        w["status"] = "ok"
        w["status_text"] = "사용 전 · 첫 요청부터 창 시작" if used < 0.5 else "리셋 시각 정보 없음"
        return w

    start = ra - sec
    elapsed = max(0.0, min(sec, now - start))
    frac = elapsed / sec if sec else 0
    w["expected"] = round(frac * 100, 1)
    if used <= 0.5:
        w["status"] = "ok"
        w["status_text"] = "거의 안 씀 · 넉넉해요"
        return w
    if elapsed < 120:
        w["status"] = "ok"
        w["status_text"] = "창이 막 시작됨"
        return w
    rate = used / elapsed
    eta = now + (100.0 - used) / rate
    projected = used / frac if frac > 0 else used
    lead = "이 속도면"
    pat = w.get("pattern")
    if pat and pat.get("end") is not None:
        # 주간 한도는 요일·시간대 사용 패턴을 반영한 예측으로 대체
        projected = pat["end"]
        eta = pat["eta"] if pat.get("eta") else ra + 1
        lead = "평소 패턴대로면"
    w["projected"] = round(min(projected, 999), 0)
    if eta < ra:
        w["eta_empty"] = eta
        early = ra - eta
        w["status"] = "danger" if (eta - now) < 3600 or used >= 85 else "warn"
        w["status_text"] = f"{lead} {fmt_when(eta, now)}쯤 바닥 (리셋보다 {fmt_dur(early, coarse=True)} 빨리)"
    elif used > w["expected"] + 12:
        w["status"] = "fast"
        w["status_text"] = f"페이스 빠름 · 리셋 때 약 {round(min(projected, 100))}% 예상"
    elif projected >= 88:
        w["status"] = "fast"
        w["status_text"] = f"빠듯해요 · 리셋 때 약 {round(min(projected, 100))}% 예상"
    else:
        w["status"] = "ok"
        w["status_text"] = f"여유 있어요 · 리셋 때 약 {round(min(projected, 100))}% 예상"
    return w


# ─────────────────────────────────────────────────────────────
#  Claude
# ─────────────────────────────────────────────────────────────
def claude_credentials():
    """(access_token, expires_at, plan_label) — 없으면 (None, None, None)."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok, None, None
    data = read_json(CLAUDE_DIR / ".credentials.json")
    if data is None and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout.strip())
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None, None, None
    o = data.get("claudeAiOauth") or {}
    return o.get("accessToken"), parse_ts(o.get("expiresAt")), plan_from_tier(o.get("rateLimitTier"), o.get("subscriptionType"))


def plan_from_tier(tier, sub=None, caps=None):
    tier = (tier or "").lower()
    sub = (sub or "").lower()
    caps = [str(c).lower() for c in (caps or [])]
    if "20x" in tier:
        return "Max 20x"
    if "5x" in tier:
        return "Max 5x"
    if sub:
        return sub.capitalize()
    if "claude_max" in caps:
        return "Max"
    if "claude_pro" in caps:
        return "Pro"
    return None


def claude_windows_from_payload(d):
    """/api/oauth/usage · claude.ai usage · statusline rate_limits 모두 처리."""
    out = []
    names = {
        "five_hour": ("5h", "5시간 세션", 5 * 3600, True),
        "seven_day": ("7d", "주간 한도 (전체)", 7 * 86400, True),
        "seven_day_opus": ("7d_opus", "주간 · Opus", 7 * 86400, False),
        "seven_day_sonnet": ("7d_sonnet", "주간 · Sonnet", 7 * 86400, False),
    }
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        if k in names:
            key, label, sec, primary = names[k]
        elif k.startswith("seven_day_"):
            key, label, sec, primary = k, "주간 · " + k[len("seven_day_"):].replace("_", " ").title(), 7 * 86400, False
        else:
            continue
        used = v.get("utilization", v.get("used_percentage", v.get("used_percent")))
        if used is None:
            continue
        ra = parse_ts(v.get("resets_at"))
        if not primary and not ra and float(used or 0) == 0:
            continue
        out.append(make_window(key, label, used, ra, sec, primary))
    order = {"5h": 0, "7d": 1}
    out.sort(key=lambda w: (order.get(w["key"], 5), w["key"]))
    return out


def claude_extra_from_payload(d):
    ex = d.get("extra_usage")
    if isinstance(ex, dict) and ex.get("is_enabled"):
        used = ex.get("used_credits")
        lim = ex.get("monthly_limit")
        if used is not None and lim:
            return f"추가 사용량 {used}/{lim} 크레딧"
        return "추가 사용량 켜짐"
    return None


def claude_web_headers(key):
    return {
        "Cookie": f"sessionKey={key}",
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "anthropic-client-platform": "web_claude_ai",
        "Referer": "https://claude.ai/settings/usage",
    }


def pick_org(orgs):
    if not isinstance(orgs, list) or not orgs:
        raise RuntimeError("claude.ai 조직 정보를 못 찾았어요")

    def score(o):
        caps = [str(c).lower() for c in (o.get("capabilities") or [])]
        return (("claude_max" in caps) * 2 + ("claude_pro" in caps) + ("chat" in caps))
    return max((o for o in orgs if isinstance(o, dict) and o.get("uuid")), key=score)


def claude_web_fetch(key, org_id=None):
    """claude.ai sessionKey로 사용량 조회 → (payload, org_id, plan)."""
    if not SESSIONKEY_RE.match(key or ""):
        raise RuntimeError("sessionKey 형식이 아니에요 (sk-ant- 로 시작해야 해요)")
    h = claude_web_headers(key)
    plan = None
    if org_id and not ORG_RE.match(str(org_id)):
        org_id = None
    for attempt in range(2):
        if not org_id:
            org = pick_org(web_get_json(f"{CLAUDE_WEB}/api/organizations", h))
            org_id = str(org["uuid"])
            if not ORG_RE.match(org_id):        # [보안] 서버가 준 값으로 URL 경로를 조작하지 못하게
                raise RuntimeError("조직 ID 형식이 이상해서 요청을 멈췄어요")
            plan = clean(plan_from_tier(org.get("rate_limit_tier"), None, org.get("capabilities")), 40)
        try:
            d = web_get_json(f"{CLAUDE_WEB}/api/organizations/{org_id}/usage", h)
            return d, org_id, plan
        except urllib.error.HTTPError as e:
            if attempt == 0 and e.code in (403, 404):
                org_id = None
                continue
            raise
    raise RuntimeError("사용량 조회 실패")


def claude_web_error(e):
    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403) and not isinstance(e, CloudflareBlocked):
        return "sessionKey가 만료됐거나 잘못됐어요 — 다시 연결해 주세요"
    return http_error_text(e, "claude.ai")


def fetch_claude(force=False):
    now = now_ts()
    cache = load_cache().get("claude")
    age = now - cache["fetched_at"] if cache and cache.get("fetched_at") else None
    min_iv = FORCE_MIN_INTERVAL if force else CLAUDE_MIN_INTERVAL
    if cache and age is not None and age < min_iv and cache.get("windows"):
        return dict(cache)

    errs = []
    token, exp, plan = claude_credentials()
    if token and not (exp and exp < now):
        try:
            d = http_get_json(CLAUDE_USAGE_URL, {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": CLAUDE_UA,
                "Accept": "application/json",
            })
            wins = claude_windows_from_payload(d)
            if not wins:
                raise RuntimeError("사용량 정보가 비어 있어요 (API 키/엔터프라이즈 계정일 수 있음)")
            res = {"fetched_at": now, "source": "api", "windows": wins,
                   "plan": plan, "extra": claude_extra_from_payload(d)}
            save_cache_part("claude", res)
            return res
        except Exception as e:  # noqa
            errs.append(http_error_text(e, "Claude Code"))
    elif token:
        errs.append("Claude Code 토큰 만료 — claude 를 한 번 실행하면 갱신돼요")

    key = get_secret("claude_session_key")
    if key:
        cfg = load_config()
        try:
            d, org_id, wplan = claude_web_fetch(key, cfg.get("claude_org_id"))
            wins = claude_windows_from_payload(d)
            if not wins:
                raise RuntimeError("claude.ai 사용량 정보가 비어 있어요")
            if org_id != cfg.get("claude_org_id") or (wplan and wplan != cfg.get("claude_plan")):
                update_config(claude_org_id=org_id, claude_plan=wplan or cfg.get("claude_plan"))
            res = {"fetched_at": now, "source": "web", "windows": wins,
                   "plan": plan or wplan or cfg.get("claude_plan"), "extra": claude_extra_from_payload(d)}
            save_cache_part("claude", res)
            return res
        except Exception as e:  # noqa
            errs.append(claude_web_error(e))

    if not token and not key:
        errs.append(CLAUDE_CONNECT_HINT)
    err = errs[0] if len(errs) == 1 else " / ".join(errs[:2])
    if cache:
        cache = dict(cache)
        cache["error"] = err
        return cache
    return {"fetched_at": None, "source": None, "windows": [], "plan": plan or load_config().get("claude_plan"),
            "error": err}


def claude_project_dirs():
    seen, out = set(), []
    for p in (CLAUDE_DIR / "projects", HOME / ".config" / "claude" / "projects"):
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if p.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def scan_claude_tokens(window_start=None):
    now = now_ts()
    since = now - 7 * 86400
    midnight = local_dt(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    seen = set()
    today = today_out = week = win = 0
    by_model = {}
    files = 0
    for d in claude_project_dirs():
        for f in d.rglob("*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            files += 1
            try:
                fh = open(f, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    msg = o.get("message") if isinstance(o, dict) else None
                    if not isinstance(msg, dict):
                        continue
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = parse_ts(o.get("timestamp"))
                    if not ts or ts < since:
                        continue
                    key = (msg.get("id"), o.get("requestId"))
                    if key[0]:
                        if key in seen:
                            continue
                        seen.add(key)
                    tot = sum(int(u.get(k) or 0) for k in (
                        "input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"))
                    out = int(u.get("output_tokens") or 0)
                    week += tot
                    if window_start and ts >= window_start:
                        win += tot
                    if ts >= midnight:
                        today += tot
                        today_out += out
                        m = pretty_model(msg.get("model") or "?")
                        by_model[m] = by_model.get(m, 0) + tot
    return {"today": today, "today_output": today_out, "week": week,
            "window": win if window_start else None, "by_model": by_model, "files": files}


def pretty_model(m):
    m = clean(str(m), 40)
    for fam in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if fam in m.lower():
            v = re.findall(r"(\d+)[-.](\d+)", m)
            return fam.capitalize() + (f" {v[0][0]}.{v[0][1]}" if v else "")
    if m.startswith("<") or m == "?":
        return "기타"
    return m


# ─────────────────────────────────────────────────────────────
#  Codex
# ─────────────────────────────────────────────────────────────
PLAN_NAMES = {"plus": "Plus", "pro": "Pro", "prolite": "Pro Lite", "team": "Team",
              "business": "Business", "enterprise": "Enterprise", "edu": "Edu", "free": "Free",
              "go": "Go"}


def codex_plan(p):
    if not p:
        return None
    return PLAN_NAMES.get(str(p).lower(), clean(str(p), 30).capitalize())


def codex_auth():
    d = read_json(CODEX_DIR / "auth.json") or {}
    t = d.get("tokens") or {}
    return t.get("access_token"), t.get("account_id"), bool(d.get("OPENAI_API_KEY"))


def codex_window(used, sec, reset_at):
    key, label = label_for_seconds(sec)
    return make_window(key, label, used, reset_at, sec, True)


def codex_windows_from_api(d):
    now = now_ts()
    rl = d.get("rate_limit") or {}
    out = []
    for slot in ("primary_window", "secondary_window"):
        w = rl.get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        sec = w.get("limit_window_seconds")
        ra = parse_ts(w.get("reset_at") or w.get("resets_at"))
        if not ra and w.get("reset_after_seconds") is not None:
            ra = now + float(w["reset_after_seconds"])
        out.append(codex_window(w["used_percent"], sec, ra))
    out.sort(key=lambda w: w["window_sec"] or 0)
    return out


def codex_reset_credits(d):
    rc = d.get("rate_limit_reset_credits") or d.get("rateLimitResetCredits")
    if not isinstance(rc, dict):
        return None
    n = rc.get("available_count", rc.get("availableCount"))
    exps = []
    for c in rc.get("credits") or []:
        if isinstance(c, dict):
            for k in ("expires_at", "expiresAt", "expiration", "expires", "expiry"):
                v = parse_ts(c.get(k))
                if v:
                    exps.append(v)
                    break
    try:
        n = int(n) if n is not None else (len(rc.get("credits") or []) or None)
    except (TypeError, ValueError):
        n = None
    return {"count": n, "expires_at": min(exps) if exps else None}


def codex_extra_from_api(d):
    cr = d.get("credits")
    if isinstance(cr, dict) and cr.get("has_credits") and not cr.get("unlimited"):
        bal = cr.get("balance")
        if bal not in (None, "", "0"):
            return f"크레딧 {bal}"
    return None


def codex_session_files(since=None):
    base = CODEX_DIR / "sessions"
    if not base.is_dir():
        return []
    files = []
    for f in base.rglob("rollout-*.jsonl"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if since and mt < since:
            continue
        files.append((mt, f))
    files.sort(reverse=True)
    return files


def codex_from_logs():
    """가장 최근 세션 로그의 rate_limits (마지막 Codex 사용 시점 기준)."""
    for _, f in codex_session_files()[:25]:
        for line in reversed(tail_lines(f)):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            rl = find_key(o, "rate_limits")
            if not isinstance(rl, dict):
                continue
            try:
                ev_ts = parse_ts(o.get("timestamp")) or f.stat().st_mtime
            except OSError:
                ev_ts = now_ts()
            out = []
            for slot in ("primary", "secondary"):
                w = rl.get(slot)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                sec = w.get("window_minutes")
                sec = float(sec) * 60 if sec else None
                ra = parse_ts(w.get("resets_at"))
                if not ra and w.get("resets_in_seconds") is not None:
                    ra = ev_ts + float(w["resets_in_seconds"])
                out.append(codex_window(w["used_percent"], sec, ra))
            if out:
                out.sort(key=lambda w: w["window_sec"] or 0)
                return out, ev_ts, rl.get("plan_type")
    return [], None, None


def fetch_codex(force=False):
    now = now_ts()
    cache = load_cache().get("codex")
    age = now - cache["fetched_at"] if cache and cache.get("fetched_at") else None
    min_iv = FORCE_MIN_INTERVAL if force else CODEX_MIN_INTERVAL
    if cache and cache.get("source") == "api" and age is not None and age < min_iv:
        return dict(cache)

    token, acct, has_key = codex_auth()
    err = None
    if token:
        try:
            h = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli",
                 "Accept": "application/json", "originator": "codex_cli_rs"}
            if acct and ACCOUNT_RE.match(str(acct)):
                h["ChatGPT-Account-Id"] = str(acct)
            d = http_get_json(CODEX_USAGE_URL, h)
            wins = codex_windows_from_api(d)
            if not wins:
                raise RuntimeError("사용량 정보가 비어 있어요")
            res = {"fetched_at": now, "source": "api", "windows": wins,
                   "plan": codex_plan(d.get("plan_type")), "extra": codex_extra_from_api(d),
                   "reset_credits": codex_reset_credits(d)}
            save_cache_part("codex", res)
            return res
        except Exception as e:  # noqa
            err = http_error_text(e, "Codex")
    elif has_key:
        err = "API 키 모드예요 — 구독 한도는 ChatGPT 로그인(codex login) 시에만 보여요"
    else:
        err = "Codex 로그인 정보를 못 찾았어요 (codex login)"

    wins, ev_ts, plan = codex_from_logs()
    if wins and (not cache or not cache.get("fetched_at") or ev_ts >= cache["fetched_at"]):
        return {"fetched_at": ev_ts, "source": "logs", "windows": wins,
                "plan": codex_plan(plan) or (cache or {}).get("plan"), "error": err,
                "reset_credits": (cache or {}).get("reset_credits")}
    if cache:
        cache = dict(cache)
        cache["error"] = err
        return cache
    return {"fetched_at": None, "source": None, "windows": [], "plan": None, "error": err}


def scan_codex_tokens(window_start=None):
    now = now_ts()
    since = now - 7 * 86400
    midnight = local_dt(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = today_out = week = win = 0
    by_model = {}
    files = 0
    for _, f in codex_session_files(since):
        files += 1
        seen_totals = set()
        model = None
        for line in tail_lines(f, 64 * 1024 * 1024):
            if '"model"' in line and '"turn_context"' in line:
                try:
                    model = find_key(json.loads(line), "model") or model
                except ValueError:
                    pass
                continue
            if '"token_count"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload") or {}
            if p.get("type") != "token_count":
                continue
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if total is not None:
                if total in seen_totals:
                    continue
                seen_totals.add(total)
            ts = parse_ts(o.get("timestamp"))
            if not ts or ts < since:
                continue
            tot = int(last.get("total_tokens") or
                      (int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)))
            out = int(last.get("output_tokens") or 0)
            week += tot
            if window_start and ts >= window_start:
                win += tot
            if ts >= midnight:
                today += tot
                today_out += out
                mname = clean(str(model or "Codex"), 40)
                by_model[mname] = by_model.get(mname, 0) + tot
    return {"today": today, "today_output": today_out, "week": week,
            "window": win if window_start else None, "by_model": by_model, "files": files}


# ─────────────────────────────────────────────────────────────
#  사용 기록(히스토리) — 위젯/대시보드가 돌 때마다 조금씩 쌓임
# ─────────────────────────────────────────────────────────────
_hist_lock = threading.Lock()
_hist_mem = {"mtime": None, "rows": []}


def load_history():
    with _hist_lock:
        try:
            mt = HISTORY_FILE.stat().st_mtime
        except OSError:
            _hist_mem.update(mtime=None, rows=[])
            return []
        if _hist_mem["mtime"] == mt:
            return _hist_mem["rows"]
        rows = []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if isinstance(r, dict) and "t" in r and "tool" in r:
                            rows.append(r)
                    except ValueError:
                        pass
        except OSError:
            pass
        rows.sort(key=lambda r: r["t"])
        _hist_mem.update(mtime=mt, rows=rows)
        return rows


def record_history(tid, raw):
    if DEMO or raw.get("source") not in ("api", "web", "statusline", "logs") or not raw.get("windows"):
        return
    t = raw.get("fetched_at") or now_ts()
    w = {}
    for x in raw["windows"]:
        if x.get("primary") and x["key"] in ("5h", "7d"):
            w[x["key"]] = [round(x["used"], 2), int(x["resets_at"]) if x.get("resets_at") else None]
    if not w:
        return
    rows = [r for r in load_history() if r["tool"] == tid]
    last = rows[-1] if rows else None
    if last:
        if t <= last["t"] + 60:
            return
        if last.get("w") == w and t - last["t"] < 1800:
            return
    rec = {"t": round(t), "tool": tid, "w": w}
    with _hist_lock:
        try:
            append_private(HISTORY_FILE, json.dumps(rec) + "\n")
            if HISTORY_FILE.stat().st_size > 3 * 1024 * 1024:
                keep = now_ts() - 45 * 86400
                lines = [l for l in open(HISTORY_FILE, encoding="utf-8") if '"t"' in l]
                kept = [l for l in lines if (json.loads(l).get("t") or 0) >= keep]
                fd, tmp = tempfile.mkstemp(prefix=".aiquota-", dir=str(HISTORY_FILE.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                os.replace(tmp, HISTORY_FILE)
        except Exception:
            pass


def _same_window(a, b):
    if not a or not b:
        return False
    if a[1] is None or b[1] is None:
        return a[1] is None and b[1] is None and b[0] >= a[0]
    return abs(a[1] - b[1]) < 1800


def history_stats(tid, now):
    """두 한도의 관계(5시간 창 1개 = 주간 몇 %), 5시간 최고 사용률, 소비 이벤트."""
    rows = [r for r in load_history() if r["tool"] == tid and r["t"] >= now - 35 * 86400]
    s5 = s7 = 0.0
    max5 = 0.0
    events = []
    for prev, cur in zip(rows, rows[1:]):
        p, c = prev.get("w", {}), cur.get("w", {})
        c5, p5, c7, p7 = c.get("5h"), p.get("5h"), c.get("7d"), p.get("7d")
        if c5:
            max5 = max(max5, c5[0])
        gap = cur["t"] - prev["t"]
        if gap < 3 * 3600 and _same_window(p5, c5) and _same_window(p7, c7):
            d5, d7 = c5[0] - p5[0], c7[0] - p7[0]
            if d5 > 0 and d7 >= 0:
                s5 += d5
                s7 += d7
        key = "7d" if c7 else "5h"
        pa, ca = p.get(key), c.get(key)
        if ca:
            d = (ca[0] - pa[0]) if _same_window(pa, ca) else ca[0]
            if d > 0.05 and gap < 12 * 3600:
                events.append((cur["t"] - min(gap, 1800) / 2, d))
    ratio = (s7 / s5) if (s5 >= 15 and s7 > 0) else None
    return {"ratio": ratio, "max5": max5, "events": events, "records": len(rows)}


# ─────────────────────────────────────────────────────────────
#  사용 패턴(요일 × 시간) 학습
# ─────────────────────────────────────────────────────────────
_TS_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
_ID_RE = re.compile(r'"id"\s*:\s*"(msg_[^"]+)"')


def claude_event_times(days=28):
    since = now_ts() - days * 86400
    seen, out = set(), []
    for d in claude_project_dirs():
        for f in d.rglob("*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            for line in tail_lines(f, 32 * 1024 * 1024):
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                m = _ID_RE.search(line)
                if m:
                    if m.group(1) in seen:
                        continue
                    seen.add(m.group(1))
                t = _TS_RE.search(line)
                ts = parse_ts(t.group(1)) if t else None
                if ts and ts >= since:
                    out.append((ts, 1.0))
    return out


def codex_event_times(days=28):
    since = now_ts() - days * 86400
    out = []
    for _, f in codex_session_files(since):
        last = None
        for line in tail_lines(f, 64 * 1024 * 1024):
            if '"token_count"' not in line:
                continue
            t = _TS_RE.search(line)
            ts = parse_ts(t.group(1)) if t else None
            if ts and ts >= since and (last is None or ts - last > 5):
                out.append((ts, 1.0))
                last = ts
    return out


def build_profile(sources, now, days=28):
    since = now - days * 86400
    grid = [[0.0] * 24 for _ in range(7)]
    per_day = {}
    n_total = 0
    for evs in sources:
        evs = [(t, w) for t, w in evs if t >= since and w > 0]
        if not evs:
            continue
        tot = sum(w for _, w in evs)
        conf = min(1.0, len(evs) / 60.0)
        for t, w in evs:
            dt = datetime.fromtimestamp(t)
            grid[dt.weekday()][dt.hour] += conf * w / tot
            hr = dt.hour + dt.minute / 60.0
            d = per_day.setdefault(dt.date(), [hr, hr, 0])
            d[0] = min(d[0], hr)
            d[1] = max(d[1], hr)
            d[2] += 1
        n_total += len(evs)
    total = sum(map(sum, grid))
    has = total > 0 and n_total >= 20
    if has:
        g = [[0.9 * v / total + 0.1 / 168 for v in row] for row in grid]
    else:
        g = [[1.0 / 168] * 24 for _ in range(7)]
    active = {k: v for k, v in per_day.items() if v[2] >= 3}
    wk = [v for k, v in active.items() if k.weekday() < 5]
    use = wk if len(wk) >= 3 else list(active.values())
    start = end = None
    if len(use) >= 3:
        start = math.floor(statistics.median(v[0] for v in use) * 2) / 2
        end = math.ceil((statistics.median(v[1] for v in use) + 0.25) * 2) / 2
        if end - start < 1:
            end = start + 1
    return {"grid": g, "has_data": has, "days": len(active), "start": start, "end": end,
            "day_w": [sum(r) for r in g], "events": n_total, "weekday_based": use is wk and len(wk) >= 3}


_prof_memo = {"t": 0, "v": None}


def compute_profile(now):
    if _prof_memo["v"] and now - _prof_memo["t"] < PROFILE_TTL:
        return _prof_memo["v"]
    sources = []
    for fn in (claude_event_times, codex_event_times):
        try:
            sources.append(fn())
        except Exception:
            pass
    for tid in ("claude", "codex"):
        try:
            sources.append(history_stats(tid, now)["events"])
        except Exception:
            pass
    prof = build_profile(sources, now)
    _prof_memo.update(t=now, v=prof)
    return prof


def weight_between(prof, t0, t1):
    """t0~t1 사이 '평소 사용량 가중치' 합 (한 주 전체 = 1)."""
    g = prof["grid"]
    total, t = 0.0, t0
    while t < t1:
        dt = datetime.fromtimestamp(t)
        nxt = (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
        seg_end = min(nxt, t1)
        total += g[dt.weekday()][dt.hour] * (seg_end - t) / 3600.0
        t = seg_end
    return total


def time_when(prof, t0, need, limit):
    """t0부터 가중치 need 만큼 쌓이는 시각 (limit 넘으면 None)."""
    g = prof["grid"]
    acc, t = 0.0, t0
    while t < limit:
        dt = datetime.fromtimestamp(t)
        nxt = (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
        seg_end = min(nxt, limit)
        rate = g[dt.weekday()][dt.hour] / 3600.0
        add = rate * (seg_end - t)
        if acc + add >= need and rate > 0:
            return t + (need - acc) / rate
        acc += add
        t = seg_end
    return None


def next_at(base_ts, wd, hour_f):
    dt = local_dt(base_ts)
    h = int(hour_f)
    m = int(round((hour_f - h) * 60)) % 60
    cand = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    cand += timedelta(days=(wd - cand.weekday()) % 7)
    if cand.timestamp() < base_ts:
        cand += timedelta(days=7)
    return cand.timestamp()


# ─────────────────────────────────────────────────────────────
#  리셋 · 사용 추천
# ─────────────────────────────────────────────────────────────
def _item(kind, level, title, text, short):
    return {"kind": kind, "level": level, "title": title, "text": text, "short": short}


def weekly_outlook(w, prof, now):
    if not w or w.get("reset_passed") or not w.get("resets_at") or not w.get("window_sec"):
        return None
    R = w["resets_at"]
    S = R - w["window_sec"]
    u = w["used"]
    left = R - now
    o = {"R": R, "u": u, "left": left, "exhausted": u >= 99.5, "end": None, "exhaust_at": None,
         "per_day": (100 - u) / max(left / 86400.0, 0.5)}
    if o["exhausted"]:
        return o
    elapsed = now - S
    if elapsed < 3 * 3600 or u < 1:
        return o
    a_past = weight_between(prof, max(S, now - 7 * 86400), now)
    if a_past < 0.02:
        return o
    k = u / a_past
    end = u + k * weight_between(prof, now, R)
    o["end"] = end
    if end >= 100:
        o["exhaust_at"] = time_when(prof, now, (100 - u) / k, R)
    return o


def plan_prime(tid, name, w5, prof, st, now):
    """5시간 창 시작 시각 추천 (첫 메시지 시점부터 5시간 창이 시작됨)."""
    a, b = prof.get("start"), prof.get("end")
    if a is None:
        return _item("prime", "info", "5시간 창 시작 추천",
                     "사용 기록이 3일 이상 쌓이면 평소 작업 시간에 맞춘 시작 시각을 추천해 드려요.",
                     None)
    if b <= a:
        b += 24
    L = max(1.0, b - a)
    base = math.ceil(L / 5 - 1e-9)
    best_c, best_d = base, 0.0
    for i in range(1, 10):
        d = i * 0.5
        c = math.ceil((L + d) / 5 - 1e-9)
        if c > best_c:
            best_c, best_d = c, d
    if best_d > 0:
        # 첫 창과 마지막 창이 작업시간을 비슷하게 나눠 갖도록 (단, 작업 시작 3시간 전보다 이르게는 X)
        bal = (5 * best_c - L) / 2.0
        best_d = max(best_d, min(math.floor(bal * 2) / 2, 3.0))
        if math.ceil((L + best_d) / 5 - 1e-9) != best_c:
            best_d = max(0.5, best_d - 0.5)
    s = a - best_d
    optional = st.get("max5", 0) < 70 and st.get("records", 0) > 20
    tail = " (5시간 한도엔 잘 안 걸려서 선택사항)" if optional else ""
    days_label = "평일" if prof.get("weekday_based") else "평소"

    idle = (w5 is None) or w5.get("reset_passed") or (not w5.get("resets_at") and w5.get("used", 0) < 1)
    dt = local_dt(now)
    hour_now = dt.hour + dt.minute / 60.0
    dw = prof["day_w"]
    workday = dw[dt.weekday()] >= 0.5 * max(dw)
    if best_d > 0 and idle and workday and (s - 0.25) <= hour_now < a:
        r1 = now + 5 * 3600
        return _item("prime", "act", "지금 5시간 창 열기 추천",
                     f"지금 {name}에 짧은 메시지 1개(예: '안녕') 보내두면 5시간 리셋이 {fmt_when(r1, now)}에 걸려, "
                     f"작업시간({hm(a)}~{hm(b)})에 창을 {best_c}개 쓸 수 있어요{tail}.",
                     f"지금 {name}에 한마디 보내두기 → {local_dt(r1):%H:%M}에 채워짐")
    if best_d == 0:
        return _item("prime", "ok", "5시간 창 시작 추천",
                     f"{days_label} 작업 {hm(a)}~{hm(b)} 기준 — 평소처럼 시작해도 최적이에요 (창 {base}개).",
                     None)
    resets = []
    r = s + 5
    while r < b and len(resets) < 3:
        resets.append(hm(r))
        r += 5
    return _item("prime", "info", "5시간 창 시작 추천",
                 f"{days_label} 작업 {hm(a)}~{hm(b)} 기준 — {hm(s)}쯤 짧은 메시지 1개로 창을 미리 열면 "
                 f"리셋이 {', '.join(resets)}에 걸려 작업시간에 창 {best_c}개 (그냥 시작하면 {base}개){tail}.",
                 f"{name}: 평일 {hm(s)}에 한마디 → 5시간 창 +{best_c - base}")


def plan_weekly(name, o, now):
    if o is None:
        return None
    R = o["R"]
    if o["exhausted"]:
        return _item("weekly", "warn", "주간 한도 전망",
                     f"주간 한도 소진 — {fmt_when(R, now)}에 풀려요 ({fmt_dur(R - now, coarse=True)} 후).",
                     f"{name} 이번 주 끝 · {day_hour(R, now)}에 채워져요")
    if o["end"] is None:
        return _item("weekly", "info", "주간 한도 전망",
                     f"리셋 {fmt_when(R, now)} · 남은 {100 - o['u']:.0f}%를 하루 약 {o['per_day']:.0f}%씩 쓰면 딱 맞아요.",
                     None)
    if o["exhaust_at"]:
        gap = R - o["exhaust_at"]
        return _item("weekly", "warn", "주간 한도 전망",
                     f"평소 패턴대로면 {fmt_when(o['exhaust_at'], now)}쯤 소진 → 리셋({fmt_when(R, now)})까지 "
                     f"{fmt_dur(gap, coarse=True)} 공백. 하루 {o['per_day']:.0f}% 이내로 쓰면 버텨요.",
                     f"{name} 이대로면 {day_hour(o['exhaust_at'], now)}쯤 바닥나요")
    end = min(o["end"], 99.4)
    if end >= 90:
        return _item("weekly", "warn" if end >= 97 else "info", "주간 한도 전망",
                     f"빠듯해요 — 리셋({fmt_when(R, now)}) 때 약 {end:.0f}%까지 쓸 페이스. "
                     f"하루 {o['per_day']:.0f}% 이내로 유지하면 안전해요.",
                     f"{name} 빠듯해요 · 하루 {o['per_day']:.0f}%씩만 쓰세요" if end >= 97 else None)
    msg = f"리셋({fmt_when(R, now)}) 때 약 {end:.0f}% 예상 — 넉넉해요."
    short = None
    if end < 65 and o["left"] < 36 * 3600:
        msg += f" 남는 {100 - end:.0f}%는 리셋 전에 몰아서 쓰는 게 이득."
        short = f"{name} {100 - end:.0f}% 남아요 · 채워지기 전에 쓰세요"
    return _item("weekly", "ok", "주간 한도 전망", msg, short)


def plan_coupon(name, w5, o, rc, now):
    """Codex 적립 리셋(쿠폰) 사용 추천일. 쿠폰은 '주간 한도가 바닥난 순간'에 쓰는 게 가장 이득."""
    count = rc.get("count") if rc else None
    if count == 0:
        return None
    have = f" · 보유 {count}개" if count else " · 보유 개수 미확인"
    title = "리셋 쿠폰 사용 추천" + have
    exp = rc.get("expires_at") if rc else None
    exp_txt = f" (쿠폰 만료 {fmt_when(exp, now)})" if exp else ""
    if o is None:
        return None
    R = o["R"]
    if o["exhausted"]:
        wait = R - now
        if wait >= 12 * 3600:
            return _item("coupon", "act", title,
                         f"지금 쓰세요 — 안 쓰면 {fmt_dur(wait, coarse=True)} 동안 {name}를 못 써요{exp_txt}.",
                         f"지금 {name} 리셋 쿠폰 쓰세요")
        return _item("coupon", "info", title,
                     f"쓰지 마세요 — {fmt_dur(wait, coarse=True)} 뒤({fmt_when(R, now)}) 자연 리셋돼요{exp_txt}.",
                     f"{name} 쿠폰 아끼세요 · 곧 채워져요")
    if o["exhaust_at"] and R - o["exhaust_at"] >= 12 * 3600:
        at = o["exhaust_at"]
        warn = ""
        if exp and exp < at:
            warn = f" 단, 쿠폰이 {fmt_when(exp, now)}에 만료되니 그 전에 쓰세요."
        return _item("coupon", "warn", title,
                     f"추천일: {fmt_when(at, now)} 무렵 — 평소 패턴대로면 이때 주간 한도가 바닥나요. "
                     f"그때 쓰면 리셋({fmt_when(R, now)})까지 {fmt_dur(R - at, coarse=True)} 공백을 메워요. "
                     f"미리 쓰면 남은 {100 - o['u']:.0f}%가 버려져요.{warn}",
                     f"{name} 쿠폰은 {day_hour(at, now)}쯤 쓰세요")
    if w5 and not w5.get("reset_passed") and w5.get("used", 0) >= 99.5 and o["u"] < 95:
        return _item("coupon", "info", title,
                     f"지금은 5시간 한도라 아끼세요 — {fmt_dur(w5.get('seconds_left'), coarse=True)} 뒤 풀려요. "
                     f"쿠폰은 주간 한도가 바닥날 때 쓰는 게 이득.",
                     f"{name} 쿠폰 아끼세요 · 5시간만 바닥")
    end = o["end"]
    if end is not None and end >= 95:
        return _item("coupon", "info", title,
                     f"아슬아슬해요 — 리셋({fmt_when(R, now)}) 직전에 바닥날 수도 있어요. "
                     f"실제로 바닥나고 리셋까지 12시간 이상 남았을 때만 쓰세요{exp_txt}.",
                     f"{name} 쿠폰은 바닥나면 그때 쓰세요")
    end_txt = f"리셋 때 약 {min(end, 99.4):.0f}% 예상" if end is not None else f"리셋 {fmt_when(R, now)}"
    msg = f"이번 주엔 필요 없어요 — {end_txt}. 다음 주로 아끼세요{exp_txt}."
    if exp and exp < R + 7 * 86400 and exp > now:
        msg = f"이번 주엔 필요 없지만 쿠폰이 {fmt_when(exp, now)}에 만료돼요 — 그 전에 주간 한도가 바닥나는 날 쓰세요."
    return _item("coupon", "info", title, msg, f"{name} 쿠폰은 이번 주 안 써도 돼요")


def plan_anchor(name, w7, prof, now):
    """Codex 주간 창은 리셋 뒤 '첫 요청' 시점부터 새로 시작 → 리셋 요일을 원하는 요일로 맞출 수 있음."""
    if not prof.get("has_data") or prof.get("days", 0) < 7 or prof.get("start") is None:
        return None
    dw = prof["day_w"]
    pairs = [(dw[i] + dw[(i + 1) % 7], i) for i in range(7)]
    light, i = min(pairs)
    if light > 0.7 * (2.0 / 7):
        return None
    tday = (i + 2) % 7
    thour = max(0.0, prof["start"] - 1)
    lname = f"{WEEKDAYS[i]}·{WEEKDAYS[(i + 1) % 7]}"
    title = "주간 리셋 요일 맞추기"
    ideal = f"매주 {WEEKDAYS[tday]}요일 {hm(thour)}"
    if w7 is None:
        return None
    if w7.get("reset_passed") or not w7.get("resets_at"):
        tgt = next_at(now, tday, thour)
        gap = tgt - now
        if gap <= 48 * 3600:
            return _item("anchor", "act", title,
                         f"지금 주간 창이 비어 있어요. 첫 요청을 {fmt_when(tgt, now)}에 하면 이후 리셋이 {ideal}로 "
                         f"정렬돼요 ({lname}요일 사용이 적어서 주 끝에 바닥나도 손해가 적음).",
                         f"{name} 첫 사용은 {day_hour(tgt, now)}에 하세요")
        return _item("anchor", "info", title,
                     f"이상적인 주간 리셋: {ideal} ({lname}요일 사용이 적음). 지금 첫 요청하면 이번 리셋은 "
                     f"{WEEKDAYS[local_dt(now).weekday()]}요일로 잡혀요.", None)
    R = w7["resets_at"]
    tgt = next_at(R, tday, thour)
    prev = tgt - 7 * 86400
    if abs(R - prev) <= 8 * 3600 or tgt - R <= 3600:
        return _item("anchor", "ok", title, f"이미 이상적인 요일({WEEKDAYS[tday]}요일)에 리셋돼요 👍", None)
    gap = tgt - R
    if gap <= 36 * 3600:
        return _item("anchor", "info", title,
                     f"리셋({fmt_when(R, now)}) 뒤 첫 요청을 {fmt_when(tgt, now)}까지 미루면 이후 {ideal} 리셋으로 "
                     f"정렬돼요 (공백 {fmt_dur(gap, coarse=True)}).", None)
    return _item("anchor", "info", title,
                 f"이상적인 리셋은 {ideal} ({lname}요일 사용이 적음). 지금은 {WEEKDAYS[local_dt(R).weekday()]}요일 "
                 f"리셋 — 맞추려면 리셋 뒤 {fmt_dur(gap, coarse=True)} 쉬어야 해서 굳이 권하진 않아요.", None)


def make_plan(t, prof, st, now):
    wins = {w["key"]: w for w in t["windows"] if w.get("primary")}
    w5, w7 = wins.get("5h"), wins.get("7d")
    name = t["name"]
    items = []
    o = weekly_outlook(w7, prof, now)
    if o and o.get("end") is not None:
        w7["pattern"] = {"eta": o["exhaust_at"], "end": o["end"]}

    if t["id"] == "codex":
        c = plan_coupon(name, w5, o, t.get("reset_credits"), now)
        if c:
            items.append(c)
    wk = plan_weekly(name, o, now)
    if wk:
        items.append(wk)

    # 두 한도 반영: 실제 가용량은 둘 중 작은 쪽
    if w5 and w7 and not w7.get("reset_passed"):
        r5, r7 = w5["remaining"], w7["remaining"]
        if r7 + 0.5 < r5:
            items.append(_item("binding", "warn" if r7 < 25 else "info", "두 한도 중 발목",
                               f"5시간은 {r5:.0f}% 남았지만 주간이 {r7:.0f}%뿐 — 실제로 쓸 수 있는 양은 주간 기준이에요.",
                               f"{name}는 이번 주 한도가 부족해요" if r7 < 25 else None))
        ratio = st.get("ratio")
        if ratio:
            n = r7 / (100 * ratio)
            items.append(_item("ratio", "info", "주간 잔여 환산",
                               f"주간 {r7:.0f}% ≈ 꽉 찬 5시간 창 {n:.1f}개분 (5시간 창 1개 = 주간 약 {ratio * 100:.0f}%)",
                               None))
    if w5 is not None or t["windows"]:
        p = plan_prime(t["id"], name, w5, prof, st, now)
        if p:
            items.append(p)
    if t["id"] == "codex":
        a = plan_anchor(name, w7, prof, now)
        if a:
            items.append(a)
    return items


LEVEL_RANK = {"act": 0, "warn": 1, "info": 2, "ok": 3}
KIND_RANK = {"coupon": 0, "prime": 1, "weekly": 2, "binding": 3, "anchor": 4, "ratio": 5}


def pick_headlines(tools):
    cands = []
    for t in tools:
        for it in t.get("reco") or []:
            if it.get("short"):
                cands.append(dict(it, tool=t["id"]))
    cands.sort(key=lambda x: (LEVEL_RANK.get(x["level"], 9), KIND_RANK.get(x["kind"], 9)))
    out = cands[:1]
    coupon = next((c for c in cands if c["kind"] == "coupon"), None)
    if coupon and coupon not in out:
        out.append(coupon)
    # 두 번째 줄은 가능하면 다른 도구 이야기로 (같은 내용 반복 방지)
    for c in cands:
        if len(out) >= 2:
            break
        if c not in out and all(c["tool"] != o["tool"] for o in out):
            out.append(c)
    for c in cands:
        if len(out) >= 2:
            break
        if c not in out and not (c["kind"] == "weekly" and any(o["kind"] == "coupon" and o["tool"] == c["tool"] for o in out)):
            out.append(c)
    return [{"text": c["short"], "level": c["level"], "tool": c["tool"], "kind": c["kind"]} for c in out[:2]]


# ─────────────────────────────────────────────────────────────
#  스냅샷 조립 + 조언
# ─────────────────────────────────────────────────────────────
_local_memo = {}
_snap_lock = threading.Lock()


def memo_scan(name, fn, window_start):
    key = (name, int(window_start or 0) // 60)
    hit = _local_memo.get(name)
    if hit and hit[0] == key and now_ts() - hit[1] < LOCAL_SCAN_TTL:
        return hit[2]
    try:
        val = fn(window_start)
    except Exception:
        val = None
    _local_memo[name] = (key, now_ts(), val)
    return val


SOURCE_LABELS = {
    "api": "실시간 API",
    "web": "claude.ai 연결",
    "statusline": "Claude Code 상태줄",
    "logs": "세션 로그",
    "demo": "데모 데이터",
    None: "데이터 없음",
}


def build_tool(tid, name, raw, tokens_fn, now, with_tokens=True):
    wins = [analyze_window(w, now) for w in raw.get("windows") or []]
    five = next((w for w in wins if w["key"] == "5h"), None)
    win_start = None
    if five and five.get("resets_at") and five.get("window_sec"):
        win_start = five["resets_at"] - five["window_sec"]
    tokens = memo_scan(tid, tokens_fn, win_start) if (with_tokens and tokens_fn) else None
    fa = raw.get("fetched_at")
    binding = None
    if wins:
        prim = [w for w in wins if w.get("primary")] or wins
        binding = min(prim, key=lambda w: w["remaining"])
    return {
        "id": tid,
        "name": name,
        "plan": clean(raw.get("plan"), 40),
        "source": raw.get("source"),
        "source_label": SOURCE_LABELS.get(raw.get("source"), raw.get("source")),
        "fetched_at": fa,
        "age": (now - fa) if fa else None,
        "error": clean(raw.get("error")),
        "extra": clean(raw.get("extra"), 80),
        "reset_credits": raw.get("reset_credits"),
        "windows": wins,
        "tokens": tokens,
        "remaining": binding["remaining"] if binding else None,
        "binding": binding["label"] if binding else None,
    }


def make_advice(tools, now):
    tips = []
    live = [t for t in tools if t["windows"]]
    if not live:
        return ["아직 읽어온 데이터가 없어요 — 위 안내대로 연결해 주세요."]

    def next_reset(t):
        rs = [w["resets_at"] for w in t["windows"] if w.get("resets_at") and w["remaining"] < 100]
        return min(rs) if rs else None

    empty = [t for t in live if (t["remaining"] or 0) <= 0.5]
    ok = [t for t in live if (t["remaining"] or 0) > 0.5]
    if empty and ok:
        for t in empty:
            nr = next_reset(t)
            other = max(ok, key=lambda x: x["remaining"])
            tips.append(f"{t['name']} 한도 소진 — {fmt_when(nr, now) if nr else '리셋'}까지는 {other['name']}로 넘기세요 "
                        f"(가용 {other['remaining']:.0f}%).")
    elif empty and not ok:
        soon = min((next_reset(t) or 9e18, t["name"]) for t in empty)
        tips.append(f"둘 다 한도 소진 — 가장 빨리 풀리는 건 {soon[1]} ({fmt_when(soon[0], now)}).")
    elif len(live) == 2:
        a, b = sorted(live, key=lambda t: -t["remaining"])
        diff = a["remaining"] - b["remaining"]
        if a["remaining"] < 15:
            tips.append("둘 다 바닥 근처 — 가벼운 작업 위주로, 큰 작업은 리셋 후로 미루세요.")
        elif diff >= 25:
            tips.append(f"지금은 {a['name']} 쪽 여유가 더 커요 (가용 {a['remaining']:.0f}% vs {b['remaining']:.0f}%) "
                        f"— 무거운 작업은 {a['name']}로.")
        else:
            tips.append(f"둘 다 여유 비슷해요 (가용 {a['remaining']:.0f}% · {b['remaining']:.0f}%) — 평소대로 쓰셔도 돼요.")
    else:
        t = live[0]
        tips.append(f"{t['name']} 가용 {t['remaining']:.0f}% ({t['binding']} 기준).")

    for t in live:
        for w in t["windows"]:
            if w.get("status") in ("danger", "warn") and w.get("eta_empty") and w["key"] == "5h":
                tips.append(f"{t['name']} {w['label']}: {w['status_text']}.")
                break
    return tips[:3]


def profile_summary(prof):
    if not prof:
        return None
    s = {"days": prof.get("days", 0), "events": prof.get("events", 0), "has_data": prof.get("has_data")}
    if prof.get("start") is not None:
        s["hours"] = f"{'평일' if prof.get('weekday_based') else '평소'} {hm(prof['start'])}~{hm(prof['end'])}"
    dw = prof.get("day_w") or []
    if dw and prof.get("has_data"):
        mx = max(dw)
        s["busy_days"] = "".join(WEEKDAYS[i] for i in range(7) if dw[i] >= 0.6 * mx)
    return s


def build_snapshot(force=False, with_tokens=True, with_plan=True):
    with _snap_lock:
        now = now_ts()
        if DEMO:
            c_raw, x_raw = demo_raw(now)
        else:
            c_raw = fetch_claude(force)
            x_raw = fetch_codex(force)
            record_history("claude", c_raw)
            record_history("codex", x_raw)
        claude = build_tool("claude", "Claude", c_raw,
                            (lambda s: demo_tokens("c")) if DEMO else scan_claude_tokens, now, with_tokens)
        codex = build_tool("codex", "Codex", x_raw,
                           (lambda s: demo_tokens("x")) if DEMO else scan_codex_tokens, now, with_tokens)
        tools = [claude, codex]
        prof = None
        if with_plan:
            prof = demo_profile(now) if DEMO else compute_profile(now)
            for t in tools:
                st = demo_stats(t["id"]) if DEMO else history_stats(t["id"], now)
                try:
                    t["reco"] = make_plan(t, prof, st, now)
                    t["windows"] = [analyze_window(w, now) for w in t["windows"]]
                except Exception as e:  # 추천이 실패해도 본 화면은 살린다
                    t["reco"] = [_item("error", "info", "추천 계산 실패", str(e), None)]
        return {"generated_at": now, "version": VERSION, "tools": tools,
                "advice": make_advice(tools, now),
                "headline": pick_headlines(tools) if with_plan else [],
                "profile": profile_summary(prof), "demo": DEMO}


# ─────────────────────────────────────────────────────────────
#  데모 데이터
# ─────────────────────────────────────────────────────────────
def demo_raw(now):
    c = {"fetched_at": now - 40, "source": "demo", "plan": "Max 5x", "extra": None, "windows": [
        make_window("5h", "5시간 세션", 63, now + 2 * 3600 + 14 * 60, 5 * 3600),
        make_window("7d", "주간 한도 (전체)", 38, now + 3 * 86400 + 5 * 3600, 7 * 86400),
        make_window("7d_sonnet", "주간 · Sonnet", 12, now + 3 * 86400 + 5 * 3600, 7 * 86400, False),
    ]}
    x = {"fetched_at": now - 70, "source": "demo", "plan": "Plus", "extra": None,
         "reset_credits": {"count": 2, "expires_at": now + 12 * 86400},
         "windows": [
             make_window("5h", "5시간 세션", 88, now + 3 * 3600 + 30 * 60, 5 * 3600),
             make_window("7d", "주간 한도", 71, now + 3 * 86400 + 4 * 3600, 7 * 86400),
         ]}
    return c, x


def demo_tokens(which):
    if which == "c":
        return {"today": 18_400_000, "today_output": 212_000, "week": 96_000_000, "window": 7_900_000,
                "by_model": {"Opus 4.8": 12_100_000, "Sonnet 4.6": 6_300_000}, "files": 14}
    return {"today": 3_150_000, "today_output": 41_000, "week": 22_700_000, "window": 1_800_000,
            "by_model": {"gpt-5.5-codex": 3_150_000}, "files": 6}


def demo_profile(now):
    evs = []
    base = local_dt(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, 29):
        day = base - timedelta(days=d)
        wd = day.weekday()
        hours = range(9, 19) if wd < 5 else (range(14, 17) if wd == 5 else [])
        for h in hours:
            for m in (5, 25, 45):
                evs.append(((day + timedelta(hours=h, minutes=m)).timestamp(), 1.0))
    return build_profile([evs], now)


def demo_stats(tid):
    return {"ratio": 0.12 if tid == "claude" else 0.2, "max5": 96, "events": [], "records": 400}


# ─────────────────────────────────────────────────────────────
#  터미널 렌더링
# ─────────────────────────────────────────────────────────────
class C:
    on = True

    @staticmethod
    def rgb(r, g, b, s):
        return f"\x1b[38;2;{r};{g};{b}m{s}\x1b[0m" if C.on else s

    @staticmethod
    def bold(s):
        return f"\x1b[1m{s}\x1b[0m" if C.on else s

    @staticmethod
    def dim(s):
        return f"\x1b[2m{s}\x1b[0m" if C.on else s


ACCENT = {"claude": (217, 119, 87), "codex": (16, 185, 129)}
STATUS_COL = {"ok": (120, 200, 140), "fresh": (120, 200, 255), "fast": (240, 200, 90),
              "warn": (245, 160, 70), "danger": (240, 90, 90), "empty": (240, 70, 70)}
STATUS_ICON = {"ok": "●", "fresh": "↺", "fast": "▲", "warn": "▲", "danger": "✖", "empty": "■"}
LEVEL_COL = {"act": (138, 180, 255), "warn": (245, 170, 80), "info": (190, 195, 205), "ok": (120, 200, 140)}
LEVEL_ICON = {"act": "▶", "warn": "!", "info": "·", "ok": "✓"}


def sev_color(used):
    """사용률 → 초록·노랑·빨강 그라데이션."""
    t = max(0.0, min(1.0, used / 100.0))
    stops = [(0.0, (80, 200, 120)), (0.6, (235, 200, 70)), (0.85, (245, 140, 60)), (1.0, (235, 70, 70))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            k = (t - a) / (b - a) if b > a else 0
            return tuple(int(ca[i] + (cb[i] - ca[i]) * k) for i in range(3))
    return stops[-1][1]


def dwidth(s):
    s = re.sub(r"\x1b\[[0-9;]*m", "", s)
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    return s + " " * max(0, width - dwidth(s))


def wrap_disp(text, width):
    out, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word) if cur else word
        if dwidth(cand) > width and cur:
            out.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def bar(used, width, expected=None):
    eighths = "▏▎▍▌▋▊▉█"
    total = used / 100.0 * width
    full = int(total)
    rem = total - full
    col = sev_color(used)
    cells = []
    for i in range(width):
        if i < full:
            cells.append(C.rgb(*col, "█"))
        elif i == full and rem > 0.06:
            cells.append(C.rgb(*col, eighths[min(7, int(rem * 8))]))
        else:
            cells.append(None)
    if expected is not None:
        ei = min(width - 1, int(expected / 100.0 * width))
        if cells[ei] is None:
            cells[ei] = C.rgb(170, 170, 185, "┊")
    return "".join(c if c is not None else C.rgb(70, 72, 82, "░") for c in cells)


def render_terminal(snap):
    now = now_ts()
    cols = shutil.get_terminal_size((88, 30)).columns
    W = max(64, min(cols - 2, 96))
    barw = max(16, W - 44)
    lines = []
    title = C.bold("⚡ AI 쿼터") + C.dim("  Claude · Codex 사용량")
    ld = local_dt(now)
    clock = ld.strftime("%Y-%m-%d") + f" ({WEEKDAYS[ld.weekday()]}) " + ld.strftime("%H:%M:%S")
    if snap.get("demo"):
        clock = C.rgb(240, 200, 90, "[DEMO] ") + clock
    lines.append("")
    lines.append("  " + pad(title, W - dwidth(clock) - 2) + C.dim(clock))
    lines.append("  " + C.dim("─" * W))
    for h in snap.get("headline") or []:
        lines.append("  " + C.rgb(*LEVEL_COL.get(h["level"], (200, 200, 200)), "★ " + h["text"]))
    for tip in snap["advice"]:
        lines.append("  " + C.rgb(250, 215, 120, "💡 ") + tip)
    lines.append("")

    for t in snap["tools"]:
        ac = ACCENT[t["id"]]
        head = C.rgb(*ac, "◆ ") + C.bold(C.rgb(*ac, t["name"].upper()))
        if t["plan"]:
            head += "  " + C.dim(t["plan"])
        if t.get("remaining") is not None:
            head += "  " + C.dim("가용 ") + C.bold(C.rgb(*sev_color(100 - t["remaining"]), f"{t['remaining']:.0f}%"))
        src = t["source_label"]
        if t["age"] is not None:
            src += f" · {fmt_dur(t['age'])} 전" if t["age"] >= 60 else " · 방금"
        live = t["source"] in ("api", "web", "statusline", "demo")
        dot = C.rgb(120, 200, 140, "● ") if live else C.rgb(240, 200, 90, "● ")
        right = dot + C.dim(src)
        lines.append("  " + pad(head, W - dwidth(right)) + right)
        lines.append("  " + C.rgb(*ac, "━" * 3) + C.dim("─" * (W - 3)))

        if not t["windows"]:
            lines.append("   " + C.rgb(240, 160, 90, "⚠ " + (t["error"] or "데이터 없음")))
            lines.append("")
            continue

        for w in t["windows"]:
            lab = w["label"] if w.get("primary") else C.dim(w["label"])
            pct = f"{w['used']:5.1f}%"
            rem = f"남음 {w['remaining']:.0f}%"
            col = sev_color(w["used"])
            lines.append("   " + pad(lab, 17) + " " + bar(w["used"], barw, w.get("expected")) + " "
                         + C.bold(C.rgb(*col, pct)) + "  " + C.dim(rem))
            if w.get("resets_at"):
                reset = C.rgb(150, 190, 255, "↻ ") + fmt_when(w["resets_at"], now) + C.dim(f"  ({fmt_dur(w['seconds_left'])} 후)")
            else:
                reset = C.dim("↻ -")
            lines.append("   " + " " * 18 + reset)
            if w.get("primary"):
                sc = STATUS_COL.get(w["status"], (180, 180, 180))
                lines.append("   " + " " * 18 + C.rgb(*sc, STATUS_ICON.get(w["status"], "●") + " " + w["status_text"]))
        tk = t.get("tokens")
        if tk and (tk.get("today") or tk.get("week")):
            s = f"오늘 {fmt_tokens(tk['today'])} 토큰 (출력 {fmt_tokens(tk['today_output'])})"
            if tk.get("window"):
                s += f" · 이번 5시간 창 {fmt_tokens(tk['window'])}"
            s += f" · 7일 {fmt_tokens(tk['week'])}"
            lines.append("   " + C.dim("Σ " + s))
        if t.get("extra"):
            lines.append("   " + C.dim("+ " + t["extra"]))
        if t.get("error"):
            lines.append("   " + C.rgb(240, 180, 90, "⚠ ") + C.dim(t["error"]))
        reco = t.get("reco") or []
        if reco:
            lines.append("   " + C.bold("★ 리셋 · 사용 추천"))
            for it in reco:
                col = LEVEL_COL.get(it["level"], (200, 200, 200))
                head = C.rgb(*col, f"{LEVEL_ICON.get(it['level'], '·')} {it['title']}")
                lines.append("     " + head)
                for ln in wrap_disp(it["text"], W - 8):
                    lines.append("       " + C.dim(ln))
        lines.append("")
    prof = snap.get("profile") or {}
    if prof.get("hours"):
        lines.append("  " + C.dim(f"사용 패턴(최근 28일, 활동 {prof['days']}일): {prof['hours']}"
                                  + (f" · 주로 {prof['busy_days']}요일" if prof.get("busy_days") else "")))
    lines.append("  " + C.dim("┊ = 지금 시점의 '균등 페이스' 위치   ·   q / Ctrl+C 종료"))
    return "\n".join(lines)


def render_line(snap):
    parts = []
    for t in snap["tools"]:
        ac = ACCENT[t["id"]]
        if not t["windows"]:
            parts.append(C.rgb(*ac, t["name"]) + C.dim(" -"))
            continue
        segs = []
        for w in t["windows"]:
            if not w.get("primary"):
                continue
            tag = "5h" if w["key"] == "5h" else ("주" if w["key"].startswith("7d") else w["key"])
            col = sev_color(w["used"])
            s = C.dim(tag + " ") + C.rgb(*col, f"{w['used']:.0f}%")
            if w.get("seconds_left") is not None:
                s += C.dim("↻" + fmt_dur(w["seconds_left"], short=True))
            segs.append(s)
        parts.append(C.rgb(*ac, t["name"]) + " " + C.dim(" · ").join(segs))
    return C.dim(" │ ").join(parts)


def enable_terminal():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass


def key_pressed_quit():
    if os.name == "nt":
        try:
            import msvcrt
            while msvcrt.kbhit():
                if msvcrt.getwch().lower() in ("q", "\x1b"):
                    return True
        except Exception:
            pass
    return False


def live_view(snap, now):
    """매초 카운트다운용: 창 상태만 다시 계산."""
    live = dict(snap)
    live["tools"] = []
    for t in snap["tools"]:
        t2 = dict(t)
        t2["windows"] = [analyze_window(w, now) for w in t["windows"]]
        if t2["windows"]:
            prim = [w for w in t2["windows"] if w.get("primary")] or t2["windows"]
            b = min(prim, key=lambda w: w["remaining"])
            t2["remaining"], t2["binding"] = b["remaining"], b["label"]
        if t.get("fetched_at"):
            t2["age"] = now - t["fetched_at"]
        live["tools"].append(t2)
    live["advice"] = make_advice(live["tools"], now)
    return live


def run_watch(interval):
    enable_terminal()
    sys.stdout.write("\x1b[?25l")
    snap = build_snapshot()
    last = now_ts()
    try:
        while True:
            if now_ts() - last >= interval:
                snap = build_snapshot()
                last = now_ts()
            now = now_ts()
            out = render_terminal(live_view(snap, now))
            out += "\n  " + C.dim(f"다음 갱신까지 {max(0, int(interval - (now - last)))}초")
            sys.stdout.write("\x1b[H\x1b[2J" + out + "\n")
            sys.stdout.flush()
            for _ in range(10):
                time.sleep(0.1)
                if key_pressed_quit():
                    return
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\n")


# ─────────────────────────────────────────────────────────────
#  Claude Code statusline 연동
# ─────────────────────────────────────────────────────────────
def run_statusline():
    enable_terminal()
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if isinstance(rl, dict):
        wins = claude_windows_from_payload(rl)
        if wins:
            prev = load_cache().get("claude") or {}
            res = {"fetched_at": now_ts(), "source": "statusline", "windows": wins,
                   "plan": prev.get("plan"), "extra": prev.get("extra")}
            save_cache_part("claude", res)
            record_history("claude", res)
    now = now_ts()
    c = load_cache().get("claude") or {"windows": []}
    x = fetch_codex_quick()
    tools = [build_tool("claude", "Claude", c, None, now, False),
             build_tool("codex", "Codex", x, None, now, False)]
    model = ""
    if isinstance(data, dict):
        m = data.get("model")
        if isinstance(m, dict):
            model = clean(m.get("display_name") or "", 40)
    line = render_line({"tools": tools})
    if model:
        line = C.dim(model + " │ ") + line
    print(line)


def fetch_codex_quick():
    c = load_cache().get("codex")
    if c and c.get("fetched_at") and now_ts() - c["fetched_at"] < 3600:
        return c
    wins, ev, plan = codex_from_logs()
    return {"fetched_at": ev, "source": "logs", "windows": wins, "plan": codex_plan(plan)}


# ─────────────────────────────────────────────────────────────
#  웹 대시보드
# ─────────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 쿼터</title>
<style>
:root{
  --bg:#0b0d12; --card:rgba(255,255,255,.035); --line:rgba(255,255,255,.08);
  --tx:#e8eaf0; --tx2:#9aa0ad; --tx3:#646a78; --track:rgba(255,255,255,.07);
  --claude:#e07a5a; --codex:#19c08c; --ok:#52c98a; --warn:#f0a44a; --bad:#ef5b5b; --blue:#8ab4ff;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    --bg:#f5f4f0; --card:rgba(255,255,255,.78); --line:rgba(0,0,0,.08);
    --tx:#1b1d22; --tx2:#5b606b; --tx3:#8d929c; --track:rgba(0,0,0,.07); --blue:#3d6fd6; --warn:#c77a1c;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:"Pretendard Variable",Pretendard,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;
  background:radial-gradient(1200px 600px at 15% -10%, rgba(224,122,90,.13), transparent 60%),
             radial-gradient(1000px 600px at 95% 0%, rgba(25,192,140,.11), transparent 60%), var(--bg);
  color:var(--tx); min-height:100vh; font-feature-settings:"tnum"; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:26px;font-weight:800;letter-spacing:-.02em}
h1 span{background:linear-gradient(90deg,var(--claude),var(--codex));-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--tx2);font-size:13px;margin-top:4px}
.clock{text-align:right}
.clock .t{font-size:30px;font-weight:700;letter-spacing:-.02em}
.clock .d{color:var(--tx2);font-size:13px}
.bar{display:flex;gap:10px;align-items:stretch;margin-bottom:16px;flex-wrap:wrap}
.advice{flex:1;min-width:280px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;backdrop-filter:blur(8px)}
.heads{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.chip{font-size:13px;font-weight:700;border-radius:99px;padding:5px 11px}
.advice .tip{font-size:13.5px;line-height:1.55;color:var(--tx2)}
.advice .tip::before{content:"💡 "}
.side{display:flex;flex-direction:column;gap:6px;align-items:flex-end;justify-content:center}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--tx);border-radius:12px;padding:10px 14px;font-size:13px}
button:hover{border-color:var(--tx3)}
.meta{color:var(--tx3);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px;position:relative;overflow:hidden;backdrop-filter:blur(8px)}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--acc)}
.chead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:10px}
.name{font-size:20px;font-weight:800;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.name i{width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc)}
.plan{font-size:12px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:99px;padding:2px 9px;opacity:.9}
.avail{font-size:13px;color:var(--tx2);margin-left:4px}.avail b{font-size:15px}
.src{font-size:12px;color:var(--tx3);display:flex;align-items:center;gap:6px;text-align:right}
.src b{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block;flex:none}
.src b.stale{background:var(--warn)}
.rings{display:flex;gap:10px;justify-content:space-around;flex-wrap:wrap;margin:10px 0 4px}
.ring{flex:1;min-width:170px;display:flex;flex-direction:column;align-items:center;text-align:center;padding:6px 4px}
.ring svg{width:150px;height:150px;overflow:visible}
.ring .lab{font-size:13px;color:var(--tx2);font-weight:600;margin-top:4px}
.ring .cd{font-size:20px;font-weight:700;margin-top:4px;letter-spacing:-.01em}
.ring .rs{font-size:12px;color:var(--tx3);margin-top:1px}
.pill{display:inline-block;margin-top:8px;font-size:11.5px;padding:4px 9px;border-radius:99px;line-height:1.35;max-width:230px}
.s-ok{background:rgba(82,201,138,.12);color:var(--ok)} .s-fresh{background:rgba(138,180,255,.13);color:var(--blue)}
.s-fast{background:rgba(240,200,90,.13);color:#d9ae3e} .s-warn{background:rgba(240,164,74,.14);color:var(--warn)}
.s-danger,.s-empty{background:rgba(239,91,91,.14);color:var(--bad)}
.minor{margin-top:10px;border-top:1px dashed var(--line);padding-top:10px}
.mrow{display:grid;grid-template-columns:120px 1fr 52px;gap:10px;align-items:center;font-size:12.5px;color:var(--tx2);padding:3px 0}
.track{height:6px;border-radius:9px;background:var(--track);overflow:hidden}
.track div{height:100%;border-radius:9px}
.plan-box{margin-top:14px;border-radius:14px;background:var(--track);padding:12px 14px}
.ph{font-size:12px;font-weight:800;color:var(--tx2);margin-bottom:6px;letter-spacing:.02em}
.pi{padding:7px 0 7px 12px;border-left:3px solid var(--tx3);margin:4px 0}
.pi .pt{font-size:13px;font-weight:700}
.pi .px{font-size:12.5px;color:var(--tx2);line-height:1.5;margin-top:2px}
.lv-act{border-color:var(--blue)} .lv-act .pt{color:var(--blue)}
.lv-warn{border-color:var(--warn)} .lv-warn .pt{color:var(--warn)}
.lv-ok{border-color:var(--ok)} .lv-ok .pt{color:var(--ok)}
.chip.lv-act{background:rgba(138,180,255,.15);color:var(--blue)} .chip.lv-warn{background:rgba(240,164,74,.15);color:var(--warn)}
.chip.lv-info{background:var(--track);color:var(--tx)} .chip.lv-ok{background:rgba(82,201,138,.13);color:var(--ok)}
.tok{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}
.tok div{background:var(--track);border-radius:12px;padding:10px 12px}
.tok small{display:block;color:var(--tx3);font-size:11px;margin-bottom:2px}
.tok b{font-size:17px;font-weight:700}
.models{font-size:12px;color:var(--tx3);margin-top:8px}
.note{font-size:12px;color:var(--tx3);margin-top:10px}
.err{font-size:12.5px;color:var(--warn);margin-top:10px;background:rgba(240,164,74,.08);border-radius:10px;padding:8px 10px;line-height:1.5}
.empty{padding:34px 10px;text-align:center;color:var(--tx2);font-size:14px;line-height:1.6}
footer{margin-top:22px;color:var(--tx3);font-size:12px;text-align:center;line-height:1.7}
.demo{background:#f0c24b;color:#231c00;font-weight:700;font-size:11px;border-radius:6px;padding:2px 7px;margin-left:8px;vertical-align:middle}
@media (max-width:520px){.clock{text-align:left}.side{align-items:flex-start}.ring svg{width:130px;height:130px}.tok b{font-size:15px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>⚡ <span>AI 쿼터</span><span id="demo"></span></h1>
      <div class="sub">Claude · Codex 사용량 / 잔여량 / 리셋 시각 · 리셋 추천</div>
    </div>
    <div class="clock"><div class="t" id="clk">--:--:--</div><div class="d" id="date"></div></div>
  </header>
  <div class="bar">
    <div class="advice"><div class="heads" id="heads"></div><div id="advice"><div class="tip">불러오는 중…</div></div></div>
    <div class="side">
      <button id="rf">↻ 지금 새로고침</button>
      <span class="meta" id="next"></span>
    </div>
  </div>
  <div class="grid" id="grid"></div>
  <footer>
    <div id="prof"></div>
    링 위의 작은 점 = 지금 시점의 ‘균등 페이스’ 위치 · 호가 점을 넘으면 평소보다 빨리 쓰는 중<br>
    데이터: Claude <code>/api/oauth/usage</code> 또는 claude.ai, Codex <code>/wham/usage</code> (각 앱이 쓰는 비공식 엔드포인트) · 실패 시 로컬 로그로 대체
  </footer>
</div>
<script>
const TOKEN=(()=>{let k=new URLSearchParams(location.hash.slice(1)).get("k");try{if(k)sessionStorage.setItem("aiq",k);else k=sessionStorage.getItem("aiq")}catch(e){}if(location.hash)history.replaceState(null,"",location.pathname);return k||""})();
async function api(q){const r=await fetch("/api/usage"+q,{headers:{"X-AIQuota-Token":TOKEN},cache:"no-store",credentials:"omit"});if(r.status===403)throw new Error("auth");return r.json()}
const $=s=>document.querySelector(s);
const WD="일월화수목금토";
let snap=null, lastFetch=0, REFRESH=60;
const pad2=n=>String(n).padStart(2,"0");
function dur(s){ if(s==null)return"-"; s=Math.max(0,Math.floor(s));
  const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;
  if(d)return `${d}일 ${h}시간`; if(h)return `${h}시간 ${pad2(m)}분`; if(m)return `${m}분 ${pad2(x)}초`; return `${x}초`;}
function when(ts){ const d=new Date(ts*1000), t=new Date(); const a=new Date(t.getFullYear(),t.getMonth(),t.getDate());
  const b=new Date(d.getFullYear(),d.getMonth(),d.getDate()); const k=Math.round((b-a)/864e5);
  const day=k===0?"오늘":k===1?"내일":k===2?"모레":`${d.getMonth()+1}/${d.getDate()}(${WD[d.getDay()]})`;
  return `${day} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;}
function tok(n){n=+n||0; for(const [u,v] of [["B",1e9],["M",1e6],["K",1e3]]) if(n>=v){const x=n/v;return (x<100?x.toFixed(1):x.toFixed(0))+u} return String(n)}
function sev(u){ const st=[[0,[82,201,138]],[60,[235,200,70]],[85,[245,140,60]],[100,[235,70,70]]];
  for(let i=0;i<st.length-1;i++){const [a,ca]=st[i],[b,cb]=st[i+1]; if(u<=b){const k=(u-a)/(b-a); return `rgb(${ca.map((c,j)=>Math.round(c+(cb[j]-c)*k)).join(",")})`}} return "rgb(235,70,70)";}
function ringSVG(w){
  const R=62,C=2*Math.PI*R,u=w.used/100, col=sev(w.used);
  let tick="";
  if(w.expected!=null){const a=(w.expected/100)*2*Math.PI-Math.PI/2; const x=75+R*Math.cos(a), y=75+R*Math.sin(a);
    tick=`<circle cx="${x}" cy="${y}" r="4.2" fill="var(--tx)" stroke="var(--bg)" stroke-width="2"/>`;}
  return `<svg viewBox="0 0 150 150">
    <circle cx="75" cy="75" r="${R}" fill="none" stroke="var(--track)" stroke-width="11"/>
    <circle cx="75" cy="75" r="${R}" fill="none" stroke="${col}" stroke-width="11" stroke-linecap="round"
      stroke-dasharray="${Math.max(0.001,u*C)} ${C}" transform="rotate(-90 75 75)" style="filter:drop-shadow(0 0 6px ${col}66);transition:stroke-dasharray .8s"/>
    ${tick}
    <text x="75" y="72" text-anchor="middle" font-size="31" font-weight="800" fill="var(--tx)">${Math.round(100-w.used)}<tspan font-size="16" dx="1">%</tspan></text>
    <text x="75" y="94" text-anchor="middle" font-size="11.5" fill="var(--tx3)">남음 · ${w.used.toFixed(0)}% 사용</text>
  </svg>`;}
function analyze(w,now){
  w={...w};
  if(w.resets_at && w.resets_at<=now){w.reset_passed=true;w.used=0;w.remaining=100;w.resets_at=null;w.status="fresh";w.status_text="리셋 완료 · 다음 요청부터 새 창 시작";w.expected=null;}
  if(w.resets_at && w.window_sec && !w.reset_passed){w.expected=Math.max(0,Math.min(100,(1-(w.resets_at-now)/w.window_sec)*100));}
  w.left=w.resets_at?w.resets_at-now:null; return w;}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function render(){
  if(!snap)return;
  const now=Date.now()/1000;
  $("#demo").innerHTML=snap.demo?'<span class="demo">DEMO</span>':"";
  $("#heads").innerHTML=(snap.headline||[]).map(h=>`<span class="chip lv-${h.level}">★ ${esc(h.text)}</span>`).join("");
  $("#advice").innerHTML=(snap.advice||[]).map(t=>`<div class="tip">${esc(t)}</div>`).join("");
  const p=snap.profile||{};
  $("#prof").textContent=p.hours?`사용 패턴(최근 28일 · 활동 ${p.days}일): ${p.hours}${p.busy_days?` · 주로 ${p.busy_days}요일`:""} — 추천은 이 패턴 기준`:"사용 기록이 쌓일수록 추천이 정확해져요";
  $("#grid").innerHTML=snap.tools.map(t=>{
    const acc=`var(--${t.id})`;
    const wins=t.windows.map(w=>analyze(w,now));
    const main=wins.filter(w=>w.primary), minor=wins.filter(w=>!w.primary);
    const age=t.fetched_at?now-t.fetched_at:null;
    const live=["api","web","statusline","demo"].includes(t.source);
    const avail=main.length?Math.min(...main.map(w=>100-w.used)):null;
    let body="";
    if(!wins.length){ body=`<div class="empty">⚠ ${esc(t.error||"데이터 없음")}</div>`; }
    else{
      body=`<div class="rings">`+main.map(w=>`<div class="ring">${ringSVG(w)}
        <div class="lab">${esc(w.label)}</div>
        <div class="cd">${w.left!=null?dur(w.left):"—"}</div>
        <div class="rs">${w.resets_at?"↻ "+when(w.resets_at)+" 리셋":"리셋 시각 없음"}</div>
        <span class="pill s-${w.status}">${esc(w.status_text)}</span></div>`).join("")+`</div>`;
      if(minor.length) body+=`<div class="minor">`+minor.map(w=>`<div class="mrow"><span>${esc(w.label)}</span>
        <div class="track"><div style="width:${w.used}%;background:${sev(w.used)}"></div></div><span style="text-align:right">${w.used.toFixed(0)}%</span></div>`).join("")+`</div>`;
      if(t.reco && t.reco.length) body+=`<div class="plan-box"><div class="ph">★ 리셋 · 사용 추천</div>`+
        t.reco.map(p=>`<div class="pi lv-${p.level}"><div class="pt">${esc(p.title)}</div><div class="px">${esc(p.text)}</div></div>`).join("")+`</div>`;
      const k=t.tokens;
      if(k && (k.today||k.week)){
        body+=`<div class="tok"><div><small>오늘</small><b>${tok(k.today)}</b></div>
          <div><small>이번 5시간 창</small><b>${k.window!=null?tok(k.window):"—"}</b></div>
          <div><small>최근 7일</small><b>${tok(k.week)}</b></div></div>`;
        const bm=Object.entries(k.by_model||{}).sort((a,b)=>b[1]-a[1]).slice(0,4);
        if(bm.length) body+=`<div class="models">오늘 모델별 · ${bm.map(([m,v])=>`${esc(m)} ${tok(v)}`).join(" · ")} <span style="opacity:.7">(캐시 읽기 포함)</span></div>`;
      }
      if(t.extra) body+=`<div class="note">+ ${esc(t.extra)}</div>`;
      if(t.error) body+=`<div class="err">⚠ ${esc(t.error)}${t.source==="logs"?" — 마지막 Codex 사용 시점 기준 값이에요":""}</div>`;
    }
    return `<section class="card" style="--acc:${acc}">
      <div class="chead"><div class="name"><i></i>${esc(t.name)}${t.plan?` <span class="plan">${esc(t.plan)}</span>`:""}${avail!=null?`<span class="avail">가용 <b style="color:${sev(100-avail)}">${Math.round(avail)}%</b></span>`:""}</div>
      <div class="src"><b class="${live?"":"stale"}"></b>${esc(t.source_label||"")}${age!=null?" · "+(age<60?"방금":dur(age)+" 전"):""}</div></div>
      ${body}</section>`;
  }).join("");
  $("#next").textContent=`${Math.max(0,Math.round(REFRESH-(now-lastFetch)))}초 후 자동 갱신`;
}
async function load(force){
  try{ snap=await api(force?"?force=1":""); lastFetch=Date.now()/1000; render(); }
  catch(e){ lastFetch=Date.now()/1000; $("#advice").innerHTML= e.message==="auth"
    ? `<div class="tip">보안을 위해 이 주소로는 열 수 없어요 — 위젯 우클릭 ▸ 자세히 보기, 또는 aiquota.py --web 으로 다시 열어 주세요.</div>`
    : `<div class="tip">대시보드 서버와 연결이 끊겼어요 — 위젯이나 aiquota.py --web 이 실행 중인지 확인하세요.</div>`; }
}
function tick(){
  const d=new Date(); $("#clk").textContent=`${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  $("#date").textContent=`${d.getFullYear()}.${pad2(d.getMonth()+1)}.${pad2(d.getDate())} (${WD[d.getDay()]})`;
  const now=Date.now()/1000;
  if(snap && snap.tools.some(t=>t.windows.some(w=>w.resets_at && w.resets_at<=now && w.resets_at>now-2))) setTimeout(()=>load(true),5000);
  if(now-lastFetch>=REFRESH) load(false); else render();
}
$("#rf").onclick=()=>{ $("#rf").textContent="↻ 불러오는 중…"; load(true).then(()=>$("#rf").textContent="↻ 지금 새로고침"); };
load(false); setInterval(tick,1000); tick();
</script>
</body>
</html>
"""

WIDGET_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 쿼터 위젯</title>
<style>
:root{--bg:#14161d;--bd:#2b2f3a;--tx:#e9ebf1;--tx2:#9aa0ad;--tx3:#6a7080;--track:#262a34;--claude:#e07a5a;--codex:#19c08c;--act:#8ab4ff;--warn:#f0a44a;--ok:#52c98a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:13px "Malgun Gothic","Segoe UI",sans-serif;padding:12px 14px;font-feature-settings:"tnum";user-select:none}
.h{display:flex;justify-content:space-between;color:var(--tx3);font-size:11px;margin-bottom:6px}.h b{color:var(--tx);font-size:12px}
.t{margin:8px 0 2px}.tn{display:flex;justify-content:space-between;align-items:baseline}
.tn span{font-weight:700;font-size:13.5px}.tn i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.tn small{color:var(--tx3);font-size:10.5px;margin-left:4px}.av{font-weight:800}
.r{display:grid;grid-template-columns:34px 1fr 36px 82px;gap:6px;align-items:center;font-size:11.5px;color:var(--tx2);margin-top:4px}
.b{height:8px;border-radius:9px;background:var(--track);overflow:hidden;position:relative}.b div{height:100%;border-radius:9px}
.b em{position:absolute;top:0;bottom:0;width:2px;background:var(--tx2)}
.p{font-weight:700;text-align:right}.c{text-align:right;color:var(--tx3)}
.e{color:var(--warn);font-size:11px;margin-top:3px;line-height:1.4}
.f{border-top:1px solid var(--bd);margin-top:10px;padding-top:8px;font-size:11.5px;line-height:1.5}
.f div::before{content:"★ "}.lv-act{color:var(--act)}.lv-warn{color:var(--warn)}.lv-ok{color:var(--ok)}.lv-info{color:var(--tx)}
</style></head><body>
<div class="h"><b>AI 쿼터</b><span id="clk"></span></div><div id="w"></div><div class="f" id="f"></div>
<script>
const TOKEN=(()=>{let k=new URLSearchParams(location.hash.slice(1)).get("k");try{if(k)sessionStorage.setItem("aiq",k);else k=sessionStorage.getItem("aiq")}catch(e){}if(location.hash)history.replaceState(null,"",location.pathname);return k||""})();
async function api(q){const r=await fetch("/api/usage"+q,{headers:{"X-AIQuota-Token":TOKEN},cache:"no-store",credentials:"omit"});if(r.status===403)throw new Error("auth");return r.json()}
const $=s=>document.querySelector(s),pad2=n=>String(n).padStart(2,"0"),WD="일월화수목금토";let snap=null,last=0;
function sev(u){const st=[[0,[82,201,138]],[60,[235,200,70]],[85,[245,140,60]],[100,[235,70,70]]];for(let i=0;i<3;i++){const[a,ca]=st[i],[b,cb]=st[i+1];if(u<=b){const k=(u-a)/(b-a);return`rgb(${ca.map((c,j)=>Math.round(c+(cb[j]-c)*k)).join(",")})`}}return"rgb(235,70,70)"}
function dur(s){s=Math.max(0,Math.floor(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}일 ${h}시간`:h?`${h}시간 ${pad2(m)}분`:`${m}분`}
function ws(ts){const d=new Date(ts*1e3),t=new Date(),k=Math.round((new Date(d.getFullYear(),d.getMonth(),d.getDate())-new Date(t.getFullYear(),t.getMonth(),t.getDate()))/864e5);return`${k===0?"오늘":k===1?"내일":WD[d.getDay()]} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function draw(){if(!snap)return;const now=Date.now()/1e3;const d=new Date();$("#clk").textContent=`${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
$("#w").innerHTML=snap.tools.map(t=>{const m=t.windows.filter(w=>w.primary).map(w=>{w={...w};if(w.resets_at&&w.resets_at<=now){w.used=0;w.resets_at=null;w.done=1}return w});
const av=m.length?Math.min(...m.map(w=>100-w.used)):null;
let h=`<div class="t"><div class="tn"><div><i style="background:var(--${t.id})"></i><span>${esc(t.name)}</span><small>${esc(t.plan||"")}</small></div>${av!=null?`<div><small style="color:var(--tx3)">가용 </small><span class="av" style="color:${sev(100-av)}">${Math.round(av)}%</span></div>`:""}</div>`;
if(!m.length)h+=`<div class="e">${esc(t.error||"데이터 없음")}</div>`;
for(const w of m){const r=100-w.used;const ex=(w.resets_at&&w.window_sec)?Math.max(0,Math.min(100,(1-(w.resets_at-now)/w.window_sec)*100)):null;
h+=`<div class="r"><span>${w.key==="5h"?"5시간":"주간"}</span><div class="b"><div style="width:${r}%;background:${sev(w.used)}"></div>${ex!=null?`<em style="left:${100-ex}%"></em>`:""}</div><span class="p" style="color:${sev(w.used)}">${Math.round(r)}%</span><span class="c">${w.done?"리셋됨":w.resets_at?(w.key==="5h"?dur(w.resets_at-now):ws(w.resets_at)):"-"}</span></div>`}
return h+"</div>"}).join("");
$("#f").innerHTML=(snap.headline||[]).map(x=>`<div class="lv-${x.level}">${esc(x.text)}</div>`).join("")||`<div class="lv-info">${esc((snap.advice||[""])[0])}</div>`}
async function load(){try{snap=await api("?lite=1");last=Date.now()}catch(e){last=Date.now();if(e.message==="auth")$("#w").innerHTML='<div class="e">위젯을 다시 실행해 주세요 (보안 토큰 만료)</div>'}draw()}
load();setInterval(()=>{if(Date.now()-last>60000)load();else draw()},1000);
</script></body></html>
"""


SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Handler(BaseHTTPRequestHandler):
    """[보안] 127.0.0.1 전용 · Host 검사(DNS 리바인딩 차단) · 실행마다 새 접근 토큰 · CSP."""
    server_version = "aiquota"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, csp=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Security-Policy", csp or "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _page(self, html):
        nonce = secrets.token_urlsafe(16)
        csp = ("default-src 'none'; "
               f"script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src data:; "
               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self._send(200, html.replace("<script>", f'<script nonce="{nonce}">'), "text/html; charset=utf-8", csp)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._page(HTML)
        if u.path == "/widget":
            return self._page(WIDGET_HTML)
        if u.path == "/api/usage":
            tok = self.headers.get("X-AIQuota-Token") or ""
            if not hmac.compare_digest(tok.encode(), self.server.token.encode()):
                return self._send(403, json.dumps({"error": "token"}), "application/json; charset=utf-8")
            q = parse_qs(u.query)
            try:
                snap = build_snapshot(force="force" in q, with_tokens="lite" not in q)
                return self._send(200, json.dumps(snap, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:  # noqa
                return self._send(500, json.dumps({"error": clean(e)}, ensure_ascii=False),
                                  "application/json; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")


def start_server(port):
    for p in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            srv.daemon_threads = True
            srv.token = secrets.token_urlsafe(24)   # 이 실행에서만 유효한 접근 토큰
            return srv, p
        except OSError:
            continue
    return None, None


def server_url(srv, port, path="/"):
    # 토큰은 URL '#' 뒤(fragment)에 → 서버 로그·Referer 로 새지 않고, 페이지가 읽은 뒤 주소창에서 지움
    return f"http://127.0.0.1:{port}{path}#k={srv.token}"


def run_web(port, open_browser=True):
    enable_terminal()
    srv, port = start_server(port)
    if not srv:
        print("사용 가능한 포트를 못 찾았어요.")
        return
    url = server_url(srv, port)
    print(C.bold("⚡ AI 쿼터 웹 대시보드") + f"  →  {url}")
    print(C.dim("   이 주소(#k=… 포함)는 이번 실행에서만 유효해요. 남에게 보내지 마세요. (Ctrl+C 종료)"))
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ─────────────────────────────────────────────────────────────
#  위젯 (tkinter) — 항상 위, 테두리 없음, 드래그 이동
# ─────────────────────────────────────────────────────────────
WG = {"bg": "#14161d", "bd": "#2c303b", "tx": "#e9ebf1", "tx2": "#9aa0ad", "tx3": "#6c7282",
      "track": "#262a34", "claude": "#e07a5a", "codex": "#19c08c",
      "act": "#8ab4ff", "warn": "#f0a44a", "info": "#c9ccd4", "ok": "#52c98a", "danger": "#ef5b5b"}
TRANSP = "#010203"
FONT_CANDIDATES = ("Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "Noto Sans CJK KR",
                   "Noto Sans KR", "NanumGothic", "Segoe UI")


def hexcol(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def dpi_aware():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def startup_file():
    return Path(os.environ.get("APPDATA", str(HOME))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AIQuota_widget.cmd"


def pythonw_exe():
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def _cmd_quote(p):
    """[보안] .cmd 안에 넣을 경로: 따옴표·줄바꿈은 거부, % 는 이스케이프."""
    p = str(p)
    if any(ch in p for ch in '"\r\n'):
        raise ValueError("경로에 쓸 수 없는 문자가 있어요")
    return p.replace("%", "%%")


def set_autostart(on):
    f = startup_file()
    if on:
        d = _cmd_quote(Path(sys.executable).parent)
        script = _cmd_quote(Path(__file__).resolve())
        pyw = _cmd_quote(pythonw_exe())
        body = ("@echo off\r\nchcp 65001 >nul\r\n"
                f'set "PATH={d};{d}\\Library\\bin;%PATH%"\r\n'
                f'start "" "{pyw}" "{script}" --widget\r\n')
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
    else:
        try:
            f.unlink()
        except OSError:
            pass


LV = {"good": "#5ad08f", "mid": "#f2c14e", "low": "#f5953c", "out": "#f05d5d", "none": "#5d6372"}
TIPC = {"act": "#8ab4ff", "warn": "#f5953c", "info": "#9aa0ad", "ok": "#5ad08f"}


def level_of(rem):
    if rem is None:
        return "none", ""
    if rem < 3:
        return "out", "바닥"
    if rem < 20:
        return "low", "아껴 쓰기"
    if rem < 50:
        return "mid", "보통"
    return "good", "넉넉"


def refill_text(w, now):
    if w.get("reset_passed"):
        return "방금 다시 채워졌어요"
    ra = w.get("resets_at")
    if not ra:
        return "아직 사용 전이에요"
    left = ra - now
    if left < 3600:
        return f"{int(left // 60) + 1}분 뒤 채워져요"
    if left < 86400:
        return f"{int(left // 3600)}시간 {int(left % 3600 // 60)}분 뒤 채워져요"
    dt = local_dt(ra)
    return f"{WEEKDAYS[dt.weekday()]}요일 {dt:%H:%M}에 채워져요"


def short_error(t):
    e = t.get("error") or ""
    if t["id"] == "claude":
        if "Claude 연결 필요" in e:
            return "연결이 필요해요", "여기를 눌러 연결하기"
        if "sessionKey" in e:
            return "로그인이 만료됐어요", "여기를 눌러 다시 연결"
        if "토큰 만료" in e:
            return "로그인이 만료됐어요", "Claude Code를 한 번 실행"
        if "Cloudflare" in e:
            return "claude.ai가 잠시 막았어요", "잠시 뒤 다시 시도해요"
    else:
        if "로그인 정보를 못 찾" in e or "API 키 모드" in e:
            return "로그인이 필요해요", "터미널에서 codex login"
        if "토큰 만료" in e:
            return "로그인이 만료됐어요", "Codex를 한 번 실행"
    if "네트워크" in e:
        return "인터넷 연결 실패", "잠시 뒤 다시 시도해요"
    return "불러오지 못했어요", "마우스를 올려 이유 보기"


def render_ring(size, bg, rings, dot=None, ss=3):
    """안티앨리어싱 이중 링을 순수 파이썬으로 그려 Tk PhotoImage 데이터로 반환.
    rings: [(바깥반지름, 두께, 채움비율, 채움색, 트랙색)]  — 12시 방향에서 시계방향으로 채움."""
    def rgb(h):
        return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
    S = size
    c = S / 2.0
    bgc = rgb(bg)
    two_pi = 2 * math.pi
    offs = [((i + 0.5) / ss, (j + 0.5) / ss) for i in range(ss) for j in range(ss)]
    n = float(len(offs))
    prep = []
    lo, hi = S, 0
    for ro, th, frac, col, trk in rings:
        ri = ro - th
        rm = (ro + ri) / 2.0
        frac = max(0.0, min(1.0, frac))
        end = frac * two_pi
        caps = []
        if 0.0 < frac < 1.0:
            caps = [(0.0, -rm), (rm * math.sin(end), -rm * math.cos(end))]
        prep.append((ri, ro, end, frac, rgb(col), rgb(trk), caps, (th / 2.0) ** 2))
        lo, hi = min(lo, ri), max(hi, ro)
    dr, dc = (dot[0], rgb(dot[1])) if dot else (0, None)
    bg_hex = "#%02x%02x%02x" % bgc
    rows = []
    for py in range(S):
        row = []
        for px in range(S):
            d0 = math.hypot(px + 0.5 - c, py + 0.5 - c)
            if (d0 < lo - 1.2 or d0 > hi + 1.2) and d0 > dr + 1.2:
                row.append(bg_hex)
                continue
            r = g = b = 0
            for ox, oy in offs:
                x = px + ox - c
                y = py + oy - c
                d = math.hypot(x, y)
                col = bgc
                if dot and d <= dr:
                    col = dc
                else:
                    for ri, ro, end, frac, fc, tc, caps, hr2 in prep:
                        if ri <= d <= ro:
                            if frac >= 1.0:
                                col = fc
                            elif frac <= 0.0:
                                col = tc
                            else:
                                a = math.atan2(x, -y) % two_pi
                                if a <= end or any((x - cx) ** 2 + (y - cy) ** 2 <= hr2 for cx, cy in caps):
                                    col = fc
                                else:
                                    col = tc
                            break
                r += col[0]
                g += col[1]
                b += col[2]
            row.append("#%02x%02x%02x" % (int(r / n + .5), int(g / n + .5), int(b / n + .5)))
        rows.append("{" + " ".join(row) + "}")
    return " ".join(rows)


class SessionKeyDialog:
    """[보안] sessionKey 전용 입력창.
    - 항상 가려서(•) 표시, '보기' 기능 없음
    - 입력칸에서 복사·잘라내기·우클릭·선택 내보내기 차단 → 한 번 넣은 키를 다시 꺼낼 수 없음
    - 연결에 성공하면 클립보드와 클립보드 기록(Win+V)에서 자동 삭제
    - 닫히는 즉시 입력값을 비움"""
    BG, FG, SUB, INBG, WARN, ERR, OK = "#14161d", "#e9ebf1", "#9aa0ad", "#0d0f14", "#f5953c", "#f05d5d", "#5ad08f"

    def __init__(self, root, on_done=None, colors=None):
        import tkinter as tk
        self.tk, self.root, self.on_done = tk, root, on_done
        if colors:
            for k, v in colors.items():
                setattr(self, k, v)
        self.result = None
        self._job = None
        t = self.top = tk.Toplevel(root)
        t.title("Claude 연결하기")
        t.configure(bg=self.BG)
        t.resizable(False, False)
        try:
            t.attributes("-topmost", True)
        except tk.TclError:
            pass
        pad = 18
        f = tk.Frame(t, bg=self.BG)
        f.pack(padx=pad, pady=pad)
        font = ("Malgun Gothic", 10) if os.name == "nt" else None

        def label(text, fg=None, bold=False, top=0, wrap=360):
            fnt = (font[0], font[1] + (1 if bold else 0), "bold" if bold else "normal") if font else None
            w = tk.Label(f, text=text, bg=self.BG, fg=fg or self.FG, justify="left", anchor="w",
                         wraplength=wrap, font=fnt)
            w.pack(fill="x", pady=(top, 0))
            return w
        label("claude.ai 로그인으로 Claude 한도 읽기", bold=True)
        label("1) 크롬/엣지에서 claude.ai 로그인\n"
              "2) F12 → Application(애플리케이션) → Cookies → https://claude.ai\n"
              "3) sessionKey 값(sk-ant-sid…)을 복사한 뒤 아래 [붙여넣기]", fg=self.SUB, top=8)
        label("⚠ 이 값은 로그인 그 자체예요. 이 창 말고 다른 곳·다른 사람에게 절대 주지 마세요.",
              fg=self.WARN, top=8)
        self.var = tk.StringVar(master=t)
        self.ent = tk.Entry(f, textvariable=self.var, show="•", exportselection=False, width=40,
                            bg=self.INBG, fg=self.FG, insertbackground=self.FG, relief="flat",
                            highlightthickness=1, highlightbackground="#303544", highlightcolor="#8ab4ff")
        self.ent.pack(fill="x", pady=(12, 0), ipady=6)
        for seq in ("<<Copy>>", "<<Cut>>", "<Control-c>", "<Control-C>", "<Control-x>", "<Control-X>",
                    "<Control-Insert>", "<Shift-Delete>", "<Button-3>", "<Button-2>", "<<PasteSelection>>"):
            self.ent.bind(seq, lambda e: "break")
        self.ent.bind("<Return>", lambda e: self.connect())
        t.bind("<Escape>", lambda e: self.cancel())
        self.status = label("", fg=self.SUB, top=8)
        row = tk.Frame(f, bg=self.BG)
        row.pack(fill="x", pady=(10, 0))
        mk = dict(relief="flat", padx=12, pady=5, cursor="hand2", bd=0)
        self.b_cancel = tk.Button(row, text="취소", command=self.cancel, bg="#2a2e39", fg=self.FG,
                                  activebackground="#343948", activeforeground=self.FG, **mk)
        self.b_cancel.pack(side="right")
        self.b_ok = tk.Button(row, text="연결", command=self.connect, bg="#3d6fd6", fg="#ffffff",
                              activebackground="#4a7be0", activeforeground="#ffffff", **mk)
        self.b_ok.pack(side="right", padx=(0, 6))
        self.b_paste = tk.Button(row, text="클립보드에서 붙여넣기", command=self.paste, bg="#2a2e39", fg=self.FG,
                                 activebackground="#343948", activeforeground=self.FG, **mk)
        self.b_paste.pack(side="left")
        t.protocol("WM_DELETE_WINDOW", self.cancel)
        t.update_idletasks()
        x = root.winfo_x() - t.winfo_reqwidth() - 12
        if x < 0:
            x = root.winfo_x() + root.winfo_width() + 12
        t.geometry(f"+{max(0, x)}+{max(0, root.winfo_y())}")
        self.ent.focus_set()
        try:
            t.grab_set()
        except tk.TclError:
            pass

    def alive(self):
        try:
            return bool(self.top.winfo_exists())
        except Exception:
            return False

    def say(self, text, color=None):
        self.status.config(text=text, fg=color or self.SUB)

    def paste(self):
        try:
            txt = self.root.clipboard_get()
        except Exception:
            txt = ""
        key = normalize_session_key(txt)
        txt = None
        if not key:
            return self.say("클립보드에 sessionKey 형식(sk-ant-…)의 값이 없어요.", self.ERR)
        self.var.set(key)
        self.ent.icursor("end")
        self.say("붙여넣었어요 (가려져 있어요). [연결]을 눌러 주세요.", self.OK)

    def connect(self):
        key = normalize_session_key(self.var.get())
        if not key:
            return self.say("sessionKey 형식이 아니에요. sk-ant- 로 시작하는 값만 넣어 주세요.", self.ERR)
        for b in (self.b_ok, self.b_paste):
            b.config(state="disabled")
        self.say("확인 중… (claude.ai 에만 보내요)")

        def work():
            try:
                d, org_id, plan = claude_web_fetch(key)
                if not claude_windows_from_payload(d):
                    raise RuntimeError("사용량 정보가 비어 있어요")
                set_secret("claude_session_key", key)
                update_config(claude_org_id=org_id, claude_plan=plan)
                save_cache_part("claude", None)
                scrub = scrub_report(scrub_clipboard(key)) if os.name == "nt" or sys.platform == "darwin" else ""
                self.result = (True, plan, scrub)
            except Exception as ex:  # noqa
                self.result = (False, claude_web_error(ex), "")
        threading.Thread(target=work, daemon=True).start()
        self._job = self.top.after(200, self._poll)

    def _poll(self):
        if self.result is None:
            self._job = self.top.after(200, self._poll)
            return
        ok, info, scrub = self.result
        if not ok:
            for b in (self.b_ok, self.b_paste):
                b.config(state="normal")
            return self.say(f"연결 실패: {info}", self.ERR)
        if os.name != "nt" and sys.platform != "darwin":
            r = scrub_clipboard(normalize_session_key(self.var.get()) or "", tk_root=self.root)
            scrub = scrub_report(r) or ("✔ 클립보드에서 지웠어요" if r.get("current") else "")
        msg = "연결됐어요" + (f" · {info}" if info else "") + (f"\n\n{scrub}" if scrub else "")
        self.close(True, msg)

    def cancel(self):
        self.close(False, None)

    def close(self, ok, msg):
        self.var.set("")                     # 입력값 즉시 비우기
        try:
            self.ent.delete(0, "end")
            self.top.grab_release()
            self.top.destroy()
        except Exception:
            pass
        if self.on_done:
            self.on_done(ok, msg)


class QuotaWidget:
    W = 272          # 기본 모드 폭 (논리 px)
    W_MINI = 178     # 미니 모드 폭
    INTERVAL = 60
    TIP_EVERY = 6

    def __init__(self):
        import tkinter as tk
        from tkinter import font as tkfont
        self.tk = tk
        dpi_aware()
        self.root = root = tk.Tk()
        root.title("AI 쿼터")
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        wc = load_config().get("widget") or {}
        self.mini = bool(wc.get("compact", False))
        self.topmost = bool(wc.get("topmost", True))
        self.alpha = float(wc.get("alpha", 0.97))
        self.x, self.y = wc.get("x"), wc.get("y")
        root.overrideredirect(True)
        self.transparent = False
        if os.name == "nt":
            try:
                root.configure(bg=TRANSP)
                root.attributes("-transparentcolor", TRANSP)
                self.transparent = True
            except tk.TclError:
                pass
        base_bg = TRANSP if self.transparent else WG["bg"]
        root.configure(bg=base_bg)
        self.cv = tk.Canvas(root, bg=base_bg, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        fams = set(tkfont.families(root))
        fam = next((f for f in FONT_CANDIDATES if f in fams), "TkDefaultFont")
        nfam = next((f for f in ("Segoe UI Semibold", "Segoe UI", "SF Pro Display", "Inter") if f in fams), fam)

        def F(px, bold=False, family=None):
            return tkfont.Font(root=root, family=family or fam, size=-max(8, int(round(px * self.s))),
                               weight="bold" if bold else "normal")
        self.F = {"name": F(13.5, True), "verdict": F(11, True), "sub": F(11), "subb": F(11, True),
                  "tiny": F(10), "big": F(25, True, nfam), "pct": F(12, True, nfam),
                  "tip": F(11.5), "mbig": F(16, True, nfam), "mpct": F(10, True, nfam), "mname": F(9.5),
                  "tt": F(11.5), "ttb": F(12, True)}
        self.snap = None
        self.pending = None
        self.loading = False
        self.last_fetch = 0
        self.toast = None
        self.srv_url = None
        self.size = (0, 0)
        self._drag = None
        self._moved = False
        self._conn = None
        self.images = {}
        self.rows = []
        self.mouse_in = False
        self.hover_row = None
        self.tipwin = None
        self._tt_job = None
        self.tip_i = 0
        self.tip_t = now_ts()
        self.apply_attrs()
        cv = self.cv
        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_motion)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Double-Button-1>", lambda e: self.toggle_mini())
        cv.bind("<Button-3>", self.show_menu)
        cv.bind("<Button-2>", self.show_menu)
        cv.bind("<Enter>", self.on_enter)
        cv.bind("<Leave>", self.on_leave)
        cv.bind("<Motion>", self.on_hover)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.fetch()
        self.draw()
        root.after(1000, self.tick)

    # ── 기본 ──
    def P(self, v):
        return int(round(v * self.s))

    def apply_attrs(self):
        try:
            self.root.attributes("-topmost", self.topmost)
            self.root.attributes("-alpha", self.alpha)
        except self.tk.TclError:
            pass

    def save_cfg(self):
        update_config(widget={"x": self.x, "y": self.y, "compact": self.mini,
                              "topmost": self.topmost, "alpha": self.alpha})

    def fetch(self, force=False):
        if self.loading and not force:
            return
        self.loading = True
        self.last_fetch = now_ts()

        def work():
            try:
                snap = build_snapshot(force=force, with_tokens=False)
            except Exception as e:  # noqa
                snap = {"tools": [], "advice": [f"오류: {e}"], "headline": [], "generated_at": now_ts()}
            self.pending = snap
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        now = now_ts()
        if self.pending is not None:
            self.snap, self.pending = self.pending, None
            self.loading = False
        if not self.loading and now - self.last_fetch >= self.INTERVAL:
            self.fetch()
        if self._conn is not None:
            self.finish_connect()
        if self.topmost and int(now) % 30 == 0:
            try:
                self.root.attributes("-topmost", True)
            except self.tk.TclError:
                pass
        if self.toast and now > self.toast[1]:
            self.toast = None
        if not self.mouse_in and now - self.tip_t >= self.TIP_EVERY:
            self.tip_i += 1
            self.tip_t = now
        if not self._drag:
            self.draw()
        self.root.after(1000, self.tick)

    # ── 그리기 도구 ──
    def rrect(self, x1, y1, x2, y2, r, **kw):
        r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def ring(self, key, size, parts, dot):
        """parts: [(남은비율, 색)] 바깥→안쪽. 값이 바뀔 때만 다시 렌더."""
        P = self.P
        if len(parts) == 2:
            geo = [(size / 2.0, P(5.2)), (size / 2.0 - P(7.6), P(4.2))]
        else:
            geo = [(size / 2.0, P(6))]
        spec = tuple((round(f * 200) / 200, col) for f, col in parts)
        ck = (key, size, spec, dot)
        img = self.images.get(key)
        if img and img[0] == ck:
            return img[1]
        rings = [(ro, th, f, col, "#272b36") for (ro, th), (f, col) in zip(geo, spec)]
        data = render_ring(size, WG["bg"], rings, dot=(P(3), dot) if dot else None)
        ph = self.tk.PhotoImage(width=size, height=size)
        ph.put(data)
        self.images[key] = (ck, ph)
        return ph

    def texts(self, x, y, parts, anchor="nw"):
        """[(글자, 폰트, 색)]을 이어 그리기 → 끝 x 반환"""
        for txt, font, col in parts:
            self.cv.create_text(x, y, text=txt, anchor=anchor, font=font, fill=col)
            x += font.measure(txt)
        return x

    def clock(self, ts, now):
        dt = local_dt(ts)
        if dt.date() == local_dt(now).date():
            return f"{dt:%H:%M}"
        if (dt.date() - local_dt(now).date()).days == 1:
            return f"내일 {dt.hour}시"
        return f"{WEEKDAYS[dt.weekday()]} {dt.hour}시"

    def fit(self, txt, font, width):
        if font.measure(txt) <= width:
            return txt
        while txt and font.measure(txt + "…") > width:
            txt = txt[:-1]
        return txt + "…"

    # ── 데이터 가공 ──
    def tool_view(self, t, now):
        prim = {w["key"]: w for w in t["windows"] if w.get("primary")}
        w5, w7 = prim.get("5h"), prim.get("7d")
        ws = [w for w in (w5, w7) if w]
        rem = min(w["remaining"] for w in ws) if ws else None
        bind = min(ws, key=lambda w: (w["remaining"], w.get("seconds_left") or 9e9)) if ws else None
        lv, word = level_of(rem)
        return {"w5": w5, "w7": w7, "rem": rem, "bind": bind, "lv": lv, "word": word}

    def tips(self, snap):
        out = []
        cands = []
        for t in snap["tools"]:
            for it in t.get("reco") or []:
                if it.get("short"):
                    cands.append(it)
        cands.sort(key=lambda x: (LEVEL_RANK.get(x["level"], 9), KIND_RANK.get(x["kind"], 9)))
        for it in cands[:3]:
            out.append((it["short"], TIPC.get(it["level"], TIPC["info"])))
        r = self.routing(snap)
        if r:
            out.insert(0 if not out or out[0][1] == TIPC["info"] else len(out), r)
        return out[:4]

    def routing(self, snap):
        now = now_ts()
        vs = [(t, self.tool_view(t, now)) for t in snap["tools"]]
        live = [(t, v) for t, v in vs if v["rem"] is not None]
        if len(live) < 2:
            if len(live) == 1:
                t, v = live[0]
                return (f"{t['name']} {v['rem']:.0f}% 남았어요", TIPC["info"])
            return None
        (ta, va), (tb, vb) = sorted(live, key=lambda p: -p[1]["rem"])
        if vb["rem"] < 3 and va["rem"] >= 3:
            return (f"{tb['name']} 바닥 · 지금은 {ta['name']}로 작업하세요", TIPC["act"])
        if va["rem"] < 3:
            return ("둘 다 바닥 · 잠시 쉬어가세요", TIPC["warn"])
        if va["rem"] - vb["rem"] >= 25:
            return (f"지금은 {ta['name']}가 더 넉넉해요", TIPC["ok"])
        if vb["rem"] >= 50:
            return ("둘 다 넉넉해요", TIPC["ok"])
        return ("둘 다 비슷하게 남았어요", TIPC["info"])

    # ── 화면 ──
    def draw(self):
        cv = self.cv
        cv.delete("all")
        now = now_ts()
        snap = live_view(self.snap, now) if self.snap else None
        W, H = self.draw_mini(snap, now) if self.mini else self.draw_full(snap, now)
        bg = self.rrect(1, 1, W - 1, H - 1, self.P(16), fill=WG["bg"], outline="#2a2e39", width=1)
        cv.tag_lower(bg)
        if (W, H) != self.size:
            self.size = (W, H)
            self.place()

    def place(self):
        W, H = self.size
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.x is None or self.y is None:
            self.x, self.y = sw - W - self.P(24), self.P(64)
        self.x = max(0, min(int(self.x), sw - self.P(60)))
        self.y = max(0, min(int(self.y), sh - self.P(40)))
        self.root.geometry(f"{W}x{H}+{self.x}+{self.y}")

    def draw_full(self, snap, now):
        cv, P, F = self.cv, self.P, self.F
        W = P(self.W)
        pad = P(14)
        self.rows = []
        if not snap:
            cv.create_text(W / 2, P(38), text="불러오는 중…", font=F["sub"], fill=WG["tx2"])
            return W, P(76)
        y = P(12)
        D = P(48)
        for i, t in enumerate(snap["tools"]):
            if i:
                cv.create_line(pad, y - P(6), W - pad, y - P(6), fill="#232733")
            v = self.tool_view(t, now)
            y0 = y
            # 이중 링: 바깥 = 5시간, 안쪽 = 이번 주
            parts = []
            for w in (v["w5"], v["w7"]):
                if w:
                    parts.append((w["remaining"] / 100.0, LV[level_of(w["remaining"])[0]]))
            if not parts:
                parts = [(0.0, LV["none"]), (0.0, LV["none"])]
            img = self.ring(t["id"], D, parts, WG[t["id"]])
            cv.create_image(pad, y + P(1), image=img, anchor="nw")
            x = pad + D + P(12)
            # 1줄: 이름 + 한 단어 상태
            nx = self.texts(x, y + P(1), [(t["name"], F["name"], WG["tx"])])
            if v["word"]:
                cv.create_text(nx + P(7), y + P(3), text=v["word"], anchor="nw", font=F["verdict"], fill=LV[v["lv"]])
            if v["rem"] is not None and t.get("error"):
                ex = nx + P(7) + (F["verdict"].measure(v["word"]) if v["word"] else 0) + P(6)
                cv.create_oval(ex, y + P(8), ex + P(5), y + P(13), fill=WG["warn"], outline="")
            # 오른쪽: 큰 숫자
            if v["rem"] is not None:
                col = LV[v["lv"]]
                pw = F["pct"].measure("%")
                cv.create_text(W - pad, y + P(6), text="%", anchor="ne", font=F["pct"], fill=col)
                cv.create_text(W - pad - pw - P(1), y - P(2), text=f"{v['rem']:.0f}", anchor="ne",
                               font=F["big"], fill=col)
                cv.create_text(W - pad, y + P(31), text="남음", anchor="ne", font=F["tiny"], fill=WG["tx3"])
            else:
                cv.create_text(W - pad, y + P(2), text="–", anchor="ne", font=F["big"], fill=LV["none"])
            # 2줄: 두 한도
            if v["rem"] is not None:
                segs = []
                if v["w5"]:
                    segs += [("5시간 ", F["sub"], WG["tx3"]),
                             (f"{v['w5']['remaining']:.0f}%", F["subb"], LV[level_of(v['w5']['remaining'])[0]])]
                if v["w7"]:
                    if segs:
                        segs.append(("   ", F["sub"], WG["tx3"]))
                    segs += [("이번 주 ", F["sub"], WG["tx3"]),
                             (f"{v['w7']['remaining']:.0f}%", F["subb"], LV[level_of(v['w7']['remaining'])[0]])]
                self.texts(x, y + P(22), segs)
                rt = refill_text(v["bind"], now)
                rcol = LV["out"] if v["lv"] == "out" else WG["tx2"]
                runs = [w for w in (v["w5"], v["w7"]) if w and w.get("eta_empty") and w.get("resets_at")]
                if runs and v["lv"] != "out":
                    w = min(runs, key=lambda w: w["eta_empty"])
                    rt = f"{self.clock(w['eta_empty'], now)}쯤 바닥 → {self.clock(w['resets_at'], now)} 채워짐"
                    rcol = LV["low"]
                cv.create_text(x, y + P(38), text=self.fit(rt, F["tiny"], W - pad - x - P(34)),
                               anchor="nw", font=F["tiny"], fill=rcol)
            else:
                a, b = short_error(t)
                cv.create_text(x, y + P(22), text=a, anchor="nw", font=F["subb"], fill=WG["warn"])
                cv.create_text(x, y + P(38), text=b, anchor="nw", font=F["tiny"], fill=WG["tx2"])
            y += P(56)
            self.rows.append((y0 - P(6), y - P(4), t))
            y += P(10)
        # 한 줄 팁 (6초마다 바뀜)
        y -= P(4)
        cv.create_line(pad, y, W - pad, y, fill="#232733")
        y += P(9)
        tips = self.tips(snap)
        if not tips and not any(t["windows"] for t in snap["tools"]):
            tips = [("Claude 칸을 눌러 연결하세요", TIPC["act"])]
        if self.toast:
            tips = [(self.toast[0], TIPC["act"])]
        right_space = P(44) if (self.mouse_in or len(tips) > 1) else P(4)
        if tips:
            txt, col = tips[self.tip_i % len(tips)]
            cv.create_oval(pad, y + P(6), pad + P(7), y + P(13), fill=col, outline="")
            cv.create_text(pad + P(13), y + P(1), text=self.fit(txt, F["tip"], W - 2 * pad - P(13) - right_space),
                           anchor="nw", font=F["tip"], fill=WG["tx"])
        if self.mouse_in:
            self.draw_controls(W - pad, y + P(9))
        elif len(tips) > 1:
            n = len(tips)
            for k in range(n):
                cx = W - pad - (n - 1 - k) * P(8) - P(2)
                on = k == self.tip_i % n
                r = max(2, P(2))
                cv.create_rectangle(cx - r, y + P(9) - r, cx + r, y + P(9) + r,
                                    fill=WG["tx"] if on else "#3a3f4c", outline="")
        elif self.loading:
            cv.create_oval(W - pad - P(6), y + P(6), W - pad, y + P(12), fill=TIPC["act"], outline="")
        y += P(24)
        return W, y + P(4)

    def draw_controls(self, xr, cy):
        cv, P = self.cv, self.P
        # 닫기 ✕
        cx, r = xr - P(5), P(4)
        cv.create_rectangle(cx - P(9), cy - P(9), cx + P(9), cy + P(9), fill=WG["bg"], outline="", tags=("btn", "act_close"))
        for a, b in ((-r, -r), (-r, r)):
            cv.create_line(cx + a, cy + b, cx - a, cy - b, fill=WG["tx2"], width=max(1, P(1.6)), tags=("btn", "act_close"))
        # 새로고침
        rx = cx - P(22)
        col = TIPC["act"] if self.loading else WG["tx2"]
        cv.create_rectangle(rx - P(9), cy - P(9), rx + P(9), cy + P(9), fill=WG["bg"], outline="", tags=("btn", "act_refresh"))
        rr = P(5)
        cv.create_arc(rx - rr, cy - rr, rx + rr, cy + rr, start=60, extent=290, style="arc",
                      outline=col, width=max(1, P(1.6)), tags=("btn", "act_refresh"))
        ax, ay = rx + rr * 0.5, cy - rr * 0.87
        cv.create_polygon(ax - P(1), ay - P(3), ax + P(3.2), ay + P(0.2), ax - P(1.3), ay + P(2.5),
                          fill=col, outline="", tags=("btn", "act_refresh"))

    def draw_mini(self, snap, now):
        cv, P, F = self.cv, self.P, self.F
        W = P(self.W_MINI)
        pad = P(10)
        self.rows = []
        if not snap:
            cv.create_text(W / 2, P(22), text="불러오는 중…", font=F["tiny"], fill=WG["tx2"])
            return W, P(44)
        D = P(30)
        colw = (W - 2 * pad) / 2.0
        for i, t in enumerate(snap["tools"]):
            v = self.tool_view(t, now)
            x = pad + i * colw
            parts = [(w["remaining"] / 100.0, LV[level_of(w["remaining"])[0]]) for w in (v["w5"], v["w7"]) if w]
            if not parts:
                parts = [(0.0, LV["none"]), (0.0, LV["none"])]
            if len(parts) == 2:
                # 미니 링은 두께를 줄여서
                img = self.mini_ring(t["id"], D, parts, WG[t["id"]])
            else:
                img = self.ring(t["id"] + "_m", D, parts, WG[t["id"]])
            cv.create_image(x, P(8), image=img, anchor="nw")
            tx = x + D + P(6)
            if v["rem"] is not None:
                col = LV[v["lv"]]
                ex = self.texts(tx, P(6), [(f"{v['rem']:.0f}", F["mbig"], col)])
                cv.create_text(ex + P(1), P(11), text="%", anchor="nw", font=F["mpct"], fill=col)
            else:
                cv.create_text(tx, P(6), text="–", anchor="nw", font=F["mbig"], fill=LV["none"])
            cv.create_text(tx, P(27), text=t["name"], anchor="nw", font=F["mname"], fill=WG["tx3"])
            self.rows.append((0, P(46), t, x, x + colw))
        return W, P(46)

    def mini_ring(self, key, size, parts, dot):
        P = self.P
        spec = tuple((round(f * 200) / 200, col) for f, col in parts)
        ck = ("mini", key, size, spec)
        k = key + "_mini"
        img = self.images.get(k)
        if img and img[0] == ck:
            return img[1]
        rings = [(size / 2.0, P(3.6), spec[0][0], spec[0][1], "#272b36"),
                 (size / 2.0 - P(5.4), P(3), spec[1][0], spec[1][1], "#272b36")]
        ph = self.tk.PhotoImage(width=size, height=size)
        ph.put(render_ring(size, WG["bg"], rings, dot=(P(2.3), dot)))
        self.images[k] = (ck, ph)
        return ph

    # ── 마우스 ──
    def row_at(self, x, y):
        for r in self.rows:
            if self.mini:
                y0, y1, t, x0, x1 = r
                if x0 <= x < x1:
                    return t
            else:
                y0, y1, t = r
                if y0 <= y < y1:
                    return t
        return None

    def on_enter(self, e):
        self.mouse_in = True
        self.draw()

    def on_leave(self, e):
        self.mouse_in = False
        self.hover_row = None
        self.hide_tt()
        self.draw()

    def on_hover(self, e):
        if self._drag:
            return
        t = self.row_at(e.x, e.y)
        tid = t["id"] if t else None
        if tid != self.hover_row:
            self.hover_row = tid
            self.hide_tt()
            if t:
                self._tt_job = self.root.after(450, lambda: self.show_tt(tid))

    def detail_lines(self, t):
        now = now_ts()
        live = live_view(self.snap, now)
        t = next((x for x in live["tools"] if x["id"] == t), None)
        if not t:
            return []
        v = self.tool_view(t, now)
        lines = [(f"{t['name']}" + (f"  ·  {t['plan']}" if t.get("plan") else ""), "head"),
                 ("바깥 고리 = 5시간 한도 · 안쪽 고리 = 이번 주 한도", "dim")]
        if v["rem"] is None:
            lines.append((t.get("error") or "데이터 없음", "warn"))
            return lines
        for w, lab in ((v["w5"], "5시간"), (v["w7"], "이번 주")):
            if not w:
                continue
            when = "방금 채워짐" if w.get("reset_passed") else (
                fmt_when(w["resets_at"], now) + " 채워짐" if w.get("resets_at") else "사용 전")
            lines.append((f"{lab}   {w['remaining']:.0f}% 남음  ·  {when}", "body"))
            if w.get("status") in ("warn", "danger"):
                lines.append(("   ↳ " + w["status_text"].replace("이 속도면", "이 속도면").replace("평소 패턴대로면", "평소대로면"), "warn"))
        for it in (t.get("reco") or []):
            if it.get("short") and it["kind"] != "weekly":
                lines.append(("★ " + it["short"], "tip"))
        src = t.get("source_label") or ""
        age = t.get("age")
        lines.append((f"{src} · " + ("방금 확인" if (age or 0) < 60 else f"{fmt_dur(age, coarse=True)} 전 확인"), "dim"))
        if t.get("error"):
            lines.append(("⚠ " + t["error"], "warn"))
        return lines

    def show_tt(self, tid):
        tk = self.tk
        self.hide_tt()
        if not self.snap or self._drag:
            return
        t = next((x for x in self.snap["tools"] if x["id"] == tid), None)
        if not t:
            return
        lines = self.detail_lines(tid)
        if not lines:
            return
        tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        fr = tk.Frame(tw, bg="#0e1016", highlightthickness=1, highlightbackground="#303544")
        fr.pack()
        colors = {"head": WG["tx"], "body": WG["tx"], "warn": LV["low"], "tip": TIPC["act"], "dim": WG["tx3"]}
        for txt, kind in lines:
            tk.Label(fr, text=txt, bg="#0e1016", fg=colors[kind], justify="left", anchor="w",
                     font=self.F["ttb"] if kind == "head" else self.F["tt"], wraplength=self.P(300)
                     ).pack(fill="x", padx=self.P(12), pady=(self.P(8) if kind == "head" else 0, self.P(2)))
        tk.Frame(fr, bg="#0e1016", height=self.P(6)).pack()
        tw.update_idletasks()
        tw_w = tw.winfo_reqwidth()
        sw = self.root.winfo_screenwidth()
        x = self.x - tw_w - self.P(8) if self.x - tw_w - self.P(8) > 0 else self.x + self.size[0] + self.P(8)
        x = max(0, min(x, sw - tw_w))
        y = self.y
        tw.geometry(f"+{int(x)}+{int(y)}")
        self.tipwin = tw

    def hide_tt(self):
        if self._tt_job:
            try:
                self.root.after_cancel(self._tt_job)
            except Exception:
                pass
            self._tt_job = None
        if self.tipwin:
            try:
                self.tipwin.destroy()
            except Exception:
                pass
            self.tipwin = None

    def _hit(self):
        cur = self.cv.find_withtag("current")
        return self.cv.gettags(cur[0]) if cur else ()

    def on_press(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self._moved = False

    def on_motion(self, e):
        if not self._drag:
            return
        self.x = e.x_root - self._drag[0]
        self.y = e.y_root - self._drag[1]
        if not self._moved:
            self.hide_tt()
        self.root.geometry(f"+{self.x}+{self.y}")
        self._moved = True

    def on_release(self, e):
        moved, self._drag = self._moved, None
        if moved:
            self.save_cfg()
            return
        tags = self._hit()
        if "act_close" in tags:
            return self.quit()
        if "act_refresh" in tags:
            self.fetch(force=True)
            self.toast = ("새로 불러오는 중…", now_ts() + 3)
            return self.draw()
        t = self.row_at(e.x, e.y)
        if t and t["id"] == "claude" and not t["windows"]:
            e_ = t.get("error") or ""
            if "연결 필요" in e_ or "sessionKey" in e_:
                self.connect_claude()

    def show_menu(self, e):
        tk = self.tk
        self.hide_tt()
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="새로고침", command=lambda: self.fetch(True))
        m.add_command(label="자세히 보기 (큰 화면)", command=self.open_dashboard)
        m.add_separator()
        m.add_command(label="크게 보기" if self.mini else "작게 보기", command=self.toggle_mini)
        self.v_top = tk.BooleanVar(value=self.topmost)
        m.add_checkbutton(label="항상 맨 위에", variable=self.v_top, command=self.toggle_top)
        sub = tk.Menu(m, tearoff=0)
        self.v_alpha = tk.DoubleVar(value=self.alpha)
        for a, lab in ((1.0, "선명하게"), (0.9, "조금 투명"), (0.78, "투명"), (0.65, "많이 투명")):
            sub.add_radiobutton(label=lab, value=a, variable=self.v_alpha, command=self.set_alpha)
        m.add_cascade(label="투명도", menu=sub)
        m.add_separator()
        m.add_command(label="Claude 연결하기…", command=self.connect_claude)
        if get_secret("claude_session_key"):
            m.add_command(label="Claude 연결 끊기", command=self.disconnect_claude)
        if os.name == "nt":
            self.v_auto = tk.BooleanVar(value=startup_file().exists())
            m.add_checkbutton(label="컴퓨터 켤 때 자동 실행", variable=self.v_auto, command=self.toggle_autostart)
        m.add_separator()
        m.add_command(label="닫기", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_mini(self):
        self.mini = not self.mini
        self.hide_tt()
        self.save_cfg()
        self.draw()

    def toggle_top(self):
        self.topmost = bool(self.v_top.get())
        self.apply_attrs()
        self.save_cfg()

    def set_alpha(self):
        self.alpha = float(self.v_alpha.get())
        self.apply_attrs()
        self.save_cfg()

    def toggle_autostart(self):
        on = bool(self.v_auto.get())
        try:
            set_autostart(on)
            self.toast = ("컴퓨터 켤 때 자동으로 떠요" if on else "자동 실행을 껐어요", now_ts() + 5)
        except Exception as ex:  # noqa
            self.toast = (f"자동 실행 설정 실패: {ex}", now_ts() + 8)

    def open_dashboard(self):
        if not self.srv_url:
            srv, port = start_server(8787)
            if not srv:
                self.toast = ("큰 화면을 열지 못했어요", now_ts() + 6)
                return
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.srv_url = server_url(srv, port)
        webbrowser.open(self.srv_url)

    def connect_claude(self):
        self.hide_tt()
        if getattr(self, "_dlg", None) and self._dlg.alive():
            self._dlg.top.lift()
            return
        self.root.attributes("-topmost", False)

        def done(ok, msg):
            self.apply_attrs()
            self._dlg = None
            if ok:
                self.toast = ("Claude 연결됐어요", now_ts() + 5)
                self.fetch(force=True)
                from tkinter import messagebox
                messagebox.showinfo("Claude 연결하기", msg, parent=self.root)
        self._dlg = SessionKeyDialog(self.root, done)

    def finish_connect(self):
        pass

    def disconnect_claude(self):
        set_secret("claude_session_key", None)
        update_config(claude_org_id=None)
        save_cache_part("claude", None)
        self.toast = ("Claude 연결을 끊었어요", now_ts() + 5)
        self.fetch(force=True)

    def quit(self):
        self.hide_tt()
        self.save_cfg()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_widget():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return run_web_widget()
    QuotaWidget().run()


def run_web_widget():
    """tkinter가 없을 때: 엣지 앱 창으로 작은 위젯 띄우기."""
    enable_terminal()
    srv, port = start_server(8787)
    if not srv:
        print("사용 가능한 포트를 못 찾았어요.")
        return
    url = server_url(srv, port, "/widget")
    edge = shutil.which("msedge") or next((p for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=360,330"])
    else:
        webbrowser.open(url)
    print("위젯(웹) 실행 중 — 이 창을 닫으면 위젯도 멈춰요.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ─────────────────────────────────────────────────────────────
#  설정 · 진단
# ─────────────────────────────────────────────────────────────
def run_setup():
    import getpass
    enable_terminal()
    print(C.bold("\n⚙  AI 쿼터 — Claude 연결 설정\n"))
    tok, exp, plan = claude_credentials()
    if tok:
        print("  " + C.rgb(120, 200, 140, "✔") + " Claude Code 로그인 발견 — 이것만으로도 동작해요." + (f" (플랜: {plan})" if plan else ""))
        print(C.dim("    claude.ai 연결은 예비 경로로 추가할 수 있어요.\n"))
    else:
        print("  " + C.rgb(240, 180, 90, "!") + " 이 PC에 Claude Code 로그인 정보가 없어요 → claude.ai 로그인으로 연결합니다.\n")
    if get_secret("claude_session_key"):
        ans = input("  이미 연결돼 있어요. 다시 연결할까요? (y=다시 연결 / d=연결 해제 / Enter=취소): ").strip().lower()
        if ans == "d":
            set_secret("claude_session_key", None)
            update_config(claude_org_id=None)
            save_cache_part("claude", None)
            print("  연결을 해제했어요.")
            return
        if ans != "y":
            return
    for line in SESSIONKEY_HELP.splitlines():
        print("  " + line)
    print()
    raw = getpass.getpass("  sessionKey 붙여넣기 (입력은 화면에 안 보여요, Enter=취소): ")
    if not raw.strip():
        print("  취소했어요.")
        return
    key = normalize_session_key(raw)
    if not key:
        print("  " + C.rgb(240, 110, 90, "✘ sessionKey 형식이 아니에요 (sk-ant- 로 시작하는 값만)"))
        return
    print("  확인 중…")
    try:
        d, org_id, plan = claude_web_fetch(key)
        wins = claude_windows_from_payload(d)
        if not wins:
            raise RuntimeError("사용량 정보가 비어 있어요")
    except Exception as e:  # noqa
        print("  " + C.rgb(240, 110, 90, "✘ 연결 실패: ") + claude_web_error(e))
        return
    try:
        set_secret("claude_session_key", key)
    except Exception as e:  # noqa
        print("  " + C.rgb(240, 110, 90, f"✘ 안전하게 저장하지 못했어요: {clean(e)}"))
        return
    update_config(claude_org_id=org_id, claude_plan=plan)
    save_cache_part("claude", None)
    print("  " + C.rgb(120, 200, 140, "✔ 연결 완료") + (f" · {plan}" if plan else ""))
    rep_txt = scrub_report(scrub_clipboard(key))
    del key, raw
    for ln in rep_txt.splitlines():
        print("  " + ln)
    now = now_ts()
    for w in wins:
        if w.get("primary"):
            a = analyze_window(w, now)
            rs = fmt_when(a["resets_at"], now) if a.get("resets_at") else "-"
            print(f"     {w['label']}: {a['used']:.0f}% 사용 · 남음 {a['remaining']:.0f}% · 리셋 {rs}")
    print(C.dim(f"\n  저장 위치: {CONFIG_FILE} ({secret_storage_label()})"))


def run_doctor():
    enable_terminal()
    ok = C.rgb(120, 200, 140, "✔")
    no = C.rgb(240, 110, 90, "✘")
    print(C.bold("\n🔎 AI 쿼터 진단\n"))
    print(f"  Python {sys.version.split()[0]} · {sys.platform} · {sys.executable}")
    try:
        import tkinter  # noqa: F401
        print(f"  {ok} tkinter (위젯)")
    except ImportError:
        print(f"  {no} tkinter 없음 → 위젯은 웹 창으로 대체돼요")
    tok, exp, plan = claude_credentials()
    print(f"  {ok if tok else no} Claude Code 로그인  {C.dim(str(CLAUDE_DIR))}" + (f"  플랜: {plan}" if plan else ""))
    if exp:
        print(f"      토큰 만료: {fmt_when(exp)} " + (C.rgb(240, 110, 90, "(만료됨)") if exp < now_ts() else ""))
    key = get_secret("claude_session_key")
    print(f"  {ok if key else no} claude.ai 연결(sessionKey)" + ("" if key else C.dim("  → python aiquota.py --setup")))
    dirs = claude_project_dirs()
    print(f"  {ok if dirs else no} Claude Code 세션 로그  {C.dim(', '.join(map(str, dirs)) or '없음')}")
    t, a, k = codex_auth()
    print(f"  {ok if t else no} Codex 로그인  {C.dim(str(CODEX_DIR / 'auth.json'))}" + ("  (API 키 모드)" if (k and not t) else ""))
    fs = codex_session_files()
    print(f"  {ok if fs else no} Codex 세션 로그  {len(fs)}개")
    rows = load_history()
    print(f"  · 사용 기록  {len(rows)}건  {C.dim(str(HISTORY_FILE))}")
    print("\n  서버 조회 테스트…")
    c = fetch_claude(force=True)
    print(f"  Claude → {SOURCE_LABELS.get(c.get('source'))}, 창 {len(c['windows'])}개" + (f"  ⚠ {c['error']}" if c.get("error") else ""))
    x = fetch_codex(force=True)
    print(f"  Codex  → {SOURCE_LABELS.get(x.get('source'))}, 창 {len(x['windows'])}개" + (f"  ⚠ {x['error']}" if x.get("error") else ""))
    prof = compute_profile(now_ts())
    ps = profile_summary(prof)
    print(f"  사용 패턴 → 이벤트 {prof['events']}개, 활동 {prof['days']}일" + (f", {ps['hours']}" if ps.get("hours") else " (아직 부족)"))
    print()


# ─────────────────────────────────────────────────────────────
def main():
    global DEMO
    ap = argparse.ArgumentParser(description="Claude · Codex 사용량 / 잔여량 / 리셋 시각 + 리셋 추천")
    ap.add_argument("--widget", action="store_true", help="작은 위젯 (항상 위)")
    ap.add_argument("--watch", "-w", action="store_true", help="터미널 라이브 모드")
    ap.add_argument("--interval", type=int, default=60, help="라이브 모드 갱신 주기(초), 기본 60")
    ap.add_argument("--web", action="store_true", help="브라우저 대시보드 실행")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true", help="--web 시 브라우저 자동 열기 끔")
    ap.add_argument("--line", action="store_true", help="한 줄 요약 출력")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--statusline", action="store_true", help="Claude Code 상태줄 명령으로 사용")
    ap.add_argument("--setup", action="store_true", help="Claude 연결 설정 (claude.ai 로그인)")
    ap.add_argument("--doctor", action="store_true", help="설정·연결 진단")
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 미리보기")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 바로 조회")
    ap.add_argument("--version", action="version", version=f"aiquota {VERSION}")
    a = ap.parse_args()
    DEMO = a.demo
    if not DEMO:
        migrate_legacy_files()
    if a.no_color or os.environ.get("NO_COLOR") or (not sys.stdout or not sys.stdout.isatty()) and not a.statusline:
        C.on = False
    if a.statusline:
        return run_statusline()
    if a.widget:
        return run_widget()
    if a.setup:
        return run_setup()
    if a.doctor:
        return run_doctor()
    if a.web:
        return run_web(a.port, not a.no_browser)
    if a.watch:
        return run_watch(max(20, a.interval))
    enable_terminal()
    snap = build_snapshot(force=a.force, with_tokens=not a.line, with_plan=not a.line)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    elif a.line:
        print(render_line(snap))
    else:
        print(render_terminal(snap))


if __name__ == "__main__":
    main()
