"""보안 회귀 테스트 — 레드팀 검수에서 재현했던 공격들이 계속 막히는지 확인.

실행:  python -m unittest discover -s tests -v
(네트워크 불필요. 로컬 가짜 서버만 사용)
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="aiquota-test-")
os.environ.update(
    AIQUOTA_CACHE=f"{TMP}/cache.json", AIQUOTA_CONFIG=f"{TMP}/config.json",
    AIQUOTA_HISTORY=f"{TMP}/history.jsonl", CLAUDE_CONFIG_DIR=f"{TMP}/claude", CODEX_HOME=f"{TMP}/codex",
)
os.environ.pop("AIQUOTA_CLAUDE_SESSION_KEY", None)
sys.path.insert(0, str(ROOT))
import aiquota as a  # noqa: E402

FAKE_KEY = "sk-ant-sid01-" + "A" * 40


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class AllowLocalHttp:
    """테스트 동안만 로컬 http 서버로 요청 허용."""
    def __enter__(self):
        self.h, self.s = set(a.ALLOWED_HOSTS), set(a.ALLOWED_SCHEMES)
        a.ALLOWED_HOSTS.add("127.0.0.1")
        a.ALLOWED_SCHEMES.add("http")

    def __exit__(self, *x):
        a.ALLOWED_HOSTS.clear(); a.ALLOWED_HOSTS.update(self.h)
        a.ALLOWED_SCHEMES.clear(); a.ALLOWED_SCHEMES.update(self.s)


class TestOutboundRequests(unittest.TestCase):
    def test_redirect_does_not_forward_credentials(self):
        got = {}

        class Evil(BaseHTTPRequestHandler):
            def log_message(self, *x): pass

            def do_GET(self):
                got["auth"] = self.headers.get("Authorization")
                got["cookie"] = self.headers.get("Cookie")
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        ev = serve(Evil)

        class Redir(BaseHTTPRequestHandler):
            def log_message(self, *x): pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{ev.server_port}/steal")
                self.end_headers()
        rd = serve(Redir)
        with AllowLocalHttp():
            with self.assertRaises(urllib.error.HTTPError):
                a.http_get_json(f"http://127.0.0.1:{rd.server_port}/",
                                {"Authorization": "Bearer SECRET", "Cookie": "sessionKey=SECRET"})
        self.assertEqual(got, {}, "리다이렉트된 곳으로 인증정보가 전달됨")

    def test_only_allowlisted_https_hosts(self):
        for url in ("http://api.anthropic.com/x", "https://evil.example/x",
                    "https://user:pw@claude.ai/x", "file:///etc/passwd"):
            with self.assertRaises(RuntimeError, msg=url):
                a.http_get_json(url, {})

    def test_response_size_capped(self):
        class Big(BaseHTTPRequestHandler):
            def log_message(self, *x): pass

            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b"[" + b"1," * (a.MAX_BODY // 2 + 10) + b"1]")
        srv = serve(Big)
        with AllowLocalHttp():
            with self.assertRaises(RuntimeError):
                a.http_get_json(f"http://127.0.0.1:{srv.server_port}/", {})

    def test_curl_fallback_keeps_secrets_out_of_argv(self):
        seen = {}
        real = subprocess.run

        def spy(cmd, *x, **k):
            seen["argv"], seen["stdin"] = cmd, k.get("input", b"")
            raise OSError("stop")
        subprocess.run = spy
        try:
            with self.assertRaises(OSError):
                a.curl_get_json("https://claude.ai/api/x", a.claude_web_headers(FAKE_KEY))
        finally:
            subprocess.run = real
        if "argv" not in seen:
            self.skipTest("curl 없음")
        self.assertFalse(any(FAKE_KEY in str(c) for c in seen["argv"]), "비밀값이 명령줄 인자에 있음")
        self.assertIn(FAKE_KEY.encode(), seen["stdin"])
        self.assertIn("--max-redirs", seen["argv"])

    @unittest.skipUnless(shutil.which("curl"), "curl 없음")
    def test_curl_reads_headers_from_stdin(self):
        got = {}

        class Echo(BaseHTTPRequestHandler):
            def log_message(self, *x): pass

            def do_GET(self):
                got["cookie"] = self.headers.get("Cookie")
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":1}')
        srv = serve(Echo)
        with AllowLocalHttp():
            env_bak = {k: os.environ.pop(k) for k in list(os.environ) if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
            try:
                r = a.curl_get_json(f"http://127.0.0.1:{srv.server_port}/", {"Cookie": f"sessionKey={FAKE_KEY}"})
            finally:
                os.environ.update(env_bak)
        self.assertEqual(r, {"ok": 1})
        self.assertEqual(got["cookie"], f"sessionKey={FAKE_KEY}")

    def test_server_supplied_org_id_cannot_alter_url(self):
        calls = []

        def fake(url, h, timeout=15):
            calls.append(url)
            return [{"uuid": "../../../evil?x=", "capabilities": ["chat"]}]
        real = a.web_get_json
        a.web_get_json = fake
        try:
            with self.assertRaises(RuntimeError):
                a.claude_web_fetch(FAKE_KEY)
        finally:
            a.web_get_json = real
        self.assertEqual(len(calls), 1)

    def test_bad_session_key_rejected_before_network(self):
        for bad in ("", "hello", "sk-ant-x\r\nX-Evil: 1", "sk-ant-" + "a" * 5):
            self.assertIsNone(a.normalize_session_key(bad))
        self.assertEqual(a.normalize_session_key(f' "sessionKey={FAKE_KEY}; other=1" '), FAKE_KEY)


class TestLocalServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        a.DEMO = True
        cls.srv, cls.port = a.start_server(18900)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        a.DEMO = False
        cls.srv.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_dns_rebinding_blocked(self):
        code, _, _ = self.get("/api/usage", {"Host": "evil.example", "X-AIQuota-Token": self.srv.token})
        self.assertEqual(code, 403)
        code, _, _ = self.get("/", {"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_api_requires_token(self):
        self.assertEqual(self.get("/api/usage")[0], 403)
        self.assertEqual(self.get("/api/usage?force=1", {"X-AIQuota-Token": "wrong"})[0], 403)
        code, _, body = self.get("/api/usage?lite=1", {"X-AIQuota-Token": self.srv.token})
        self.assertEqual(code, 200)
        self.assertIn("tools", json.loads(body))

    def test_security_headers_and_csp(self):
        code, h, body = self.get("/")
        self.assertEqual(code, 200)
        csp = h.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertRegex(csp, r"script-src 'nonce-[A-Za-z0-9_\-]+'")
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">'.encode(), body)
        self.assertEqual(h.get("X-Frame-Options"), "DENY")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertNotIn(b"cdn.", body, "외부 CDN 리소스가 남아 있음")
        self.assertNotIn(b"https://", body.split(b"<footer>")[0])

    def test_token_not_in_page(self):
        _, _, body = self.get("/")
        self.assertNotIn(self.srv.token.encode(), body)


class TestLocalFilesAndOutput(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "권한 비트는 POSIX 전용 (Windows는 사용자 폴더 ACL)")
    def test_private_file_permissions_survive_updates(self):
        a.set_secret("claude_session_key", FAKE_KEY)
        a.update_config(widget={"x": 1})
        a.save_cache_part("x", {"a": 1})
        a.append_private(a.HISTORY_FILE, "{}\n")
        for f in (a.CONFIG_FILE, a.CACHE_FILE, a.HISTORY_FILE):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600, f)
        self.assertEqual(a.get_secret("claude_session_key"), FAKE_KEY)
        a.set_secret("claude_session_key", None)
        self.assertIsNone(a.get_secret("claude_session_key"))

    def test_terminal_escape_injection_stripped(self):
        evil = {"model": {"display_name": "Opus\x1b]52;c;ZWNobw==\x07\x1b[2J\u202e"},
                "rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": 4102444800}}}
        p = subprocess.run([sys.executable, str(ROOT / "aiquota.py"), "--statusline", "--no-color"],
                           input=json.dumps(evil), capture_output=True, text=True, env=os.environ, timeout=60)
        self.assertNotIn("\x1b", p.stdout)
        self.assertNotIn("\u202e", p.stdout)
        self.assertIn("Opus", p.stdout)

    def test_secrets_redacted_from_errors(self):
        msg = a.http_error_text(RuntimeError(
            "via http://user:pass@proxy:8080 sk-ant-oat01-abcdef123456 Bearer abc.def "
            "sessionKey=sk-x eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"), "x")
        for leak in ("user:pass", "abcdef123456", "abc.def", "sessionKey=sk-x", "eyJhbGci"):
            self.assertNotIn(leak, msg)

    def test_nan_values_neutralized(self):
        w = a.make_window("5h", "5h", float("nan"), float("inf"), 18000)
        self.assertEqual(w["used"], 0.0)
        self.assertIsNone(w["resets_at"])

    def test_autostart_path_escaping(self):
        self.assertEqual(a._cmd_quote(r"C:\100%\x"), r"C:\100%%\x")
        with self.assertRaises(ValueError):
            a._cmd_quote('C:\\a"b')

    def test_snapshot_never_contains_secrets(self):
        a.set_secret("claude_session_key", FAKE_KEY)
        try:
            a.DEMO = True
            dump = json.dumps(a.build_snapshot(with_tokens=False))
        finally:
            a.DEMO = False
            a.set_secret("claude_session_key", None)
        self.assertNotIn(FAKE_KEY, dump)
        self.assertNotIn("sk-ant-", dump)


class TestSessionKeyNeverExposed(unittest.TestCase):
    """sessionKey 가 어디에도 새지 않는지 — 가장 중요한 요구사항."""

    def test_clipboard_history_script_has_no_key(self):
        import base64
        import hashlib
        script = a.clipboard_history_script(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, script)
        self.assertNotIn("sk-ant-sid01", script)
        self.assertIn(hashlib.sha256(FAKE_KEY.encode()).hexdigest(), script)
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        self.assertNotIn(FAKE_KEY, base64.b64decode(enc).decode("utf-16-le"))

    def test_key_detection_in_clipboard_text(self):
        self.assertTrue(a.key_in_text(FAKE_KEY, FAKE_KEY))
        self.assertTrue(a.key_in_text(FAKE_KEY, f"sessionKey={FAKE_KEY}; lastActiveOrg=x"))
        self.assertFalse(a.key_in_text(FAKE_KEY, "sk-ant-sid01-" + "B" * 40))
        self.assertFalse(a.key_in_text(FAKE_KEY, ""))

    def test_scrub_report_never_contains_key(self):
        for res in ({"current": True, "history": 2}, {"current": False, "history": None}, {"history": "off"}):
            self.assertNotIn("sk-ant-sid01", a.scrub_report(res))

    def test_dpapi_path_uses_entropy_and_migrates_legacy(self):
        calls = []

        def fake_dpapi(data, protect, entropy=None):
            calls.append((protect, entropy))
            if protect:
                return b"ENC[" + (entropy or b"") + b"]" + data[::-1]
            head, _, body = data.partition(b"]")
            if head != b"ENC[" + (entropy or b""):
                raise OSError("wrong entropy")
            return body[::-1]
        real_use, real_dp = a._use_dpapi, a._dpapi
        a._use_dpapi, a._dpapi = (lambda: True), fake_dpapi
        try:
            import base64
            legacy = "dpapi:" + base64.b64encode(fake_dpapi(FAKE_KEY.encode(), True)).decode()
            a.update_config(claude_session_key=legacy)
            self.assertEqual(a.get_secret("claude_session_key"), FAKE_KEY)
            stored = a.load_config()["claude_session_key"]
            self.assertTrue(stored.startswith("dpapi2:"), "예전 형식이 새 형식으로 이전되지 않음")
            self.assertEqual(a.get_secret("claude_session_key"), FAKE_KEY)
            self.assertIn((True, a._ENTROPY), calls)
            self.assertNotIn(FAKE_KEY, Path(a.CONFIG_FILE).read_text(encoding="utf-8"))
        finally:
            a._use_dpapi, a._dpapi = real_use, real_dp
            a.update_config(claude_session_key=None)

    def test_dpapi_failure_never_falls_back_to_plaintext(self):
        def boom(*x, **k):
            raise OSError("no dpapi")
        real_use, real_dp = a._use_dpapi, a._dpapi
        a._use_dpapi, a._dpapi = (lambda: True), boom
        try:
            with self.assertRaises(OSError):
                a.set_secret("claude_session_key", FAKE_KEY)
            self.assertNotIn("claude_session_key", a.load_config())
        finally:
            a._use_dpapi, a._dpapi = real_use, real_dp

    def test_setup_output_never_prints_key(self):
        def fake(url, h, timeout=15):
            if url.endswith("/api/organizations"):
                return [{"uuid": "org-12345678", "capabilities": ["chat", "claude_pro"]}]
            return {"five_hour": {"utilization": 10, "resets_at": 4102444800},
                    "seven_day": {"utilization": 20, "resets_at": 4102444800}}
        script = (
            "import sys, getpass; sys.path.insert(0, %r)\n"
            "import aiquota as a\n"
            "a.web_get_json = %s\n"
            "getpass.getpass = lambda *x, **k: %r\n"
            "a.scrub_clipboard = lambda *x, **k: {'current': True, 'history': 1}\n"
            "a.run_setup()\n"
        ) % (str(ROOT), "lambda url, h, timeout=15: ([{'uuid': 'org-12345678', 'capabilities': ['chat']}] "
             "if url.endswith('/api/organizations') else {'five_hour': {'utilization': 10, 'resets_at': 4102444800}})",
             f"sessionKey={FAKE_KEY}")
        env = dict(os.environ, AIQUOTA_CONFIG=f"{TMP}/setup_cfg.json")
        p = subprocess.run([sys.executable, "-c", script], input="\n", capture_output=True, text=True, env=env, timeout=60)
        out = p.stdout + p.stderr
        self.assertIn("연결 완료", out, out[-500:])
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn("sk-ant-sid01", out)
        cfg = Path(f"{TMP}/setup_cfg.json").read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, cfg)

    def test_legacy_home_files_are_moved_and_removed(self):
        home, data = Path(TMP) / "home", Path(TMP) / "localappdata"
        home.mkdir(exist_ok=True)
        (home / ".aiquota_config.json").write_text('{"claude_session_key": "dpapi:xx"}', encoding="utf-8")
        saved = (a.HOME, a.DATA_DIR, dict(os.environ))
        try:
            for env, _, _ in a._FILES.values():
                os.environ.pop(env, None)
            a.HOME, a.DATA_DIR = home, data
            a.migrate_legacy_files()
            self.assertFalse((home / ".aiquota_config.json").exists(), "옛 위치에 키 사본이 남음")
            self.assertTrue((data / "config.json").exists())
        finally:
            a.HOME, a.DATA_DIR = saved[0], saved[1]
            os.environ.clear(); os.environ.update(saved[2])


def _tk_root():
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        return r
    except Exception:
        return None


class TestSessionKeyDialog(unittest.TestCase):
    """입력창: 가려서 표시 · 다시 꺼낼 수 없음 · 닫으면 비워짐 (tkinter + 화면 있을 때만)."""

    def setUp(self):
        self.root = _tk_root()
        if not self.root:
            self.skipTest("tkinter/디스플레이 없음")

    def tearDown(self):
        if self.root:
            self.root.destroy()

    def test_masked_and_uncopyable(self):
        d = a.SessionKeyDialog(self.root)
        self.assertEqual(d.ent.cget("show"), "•")
        self.assertIn(str(d.ent.cget("exportselection")), ("0", "False"))
        d.var.set(FAKE_KEY)
        self.root.clipboard_clear(); self.root.clipboard_append("SAFE"); self.root.update()
        d.ent.selection_range(0, "end")
        for ev in ("<<Copy>>", "<<Cut>>"):
            d.ent.event_generate(ev)
            self.root.update()
            self.assertEqual(self.root.clipboard_get(), "SAFE", ev)
        self.assertEqual(d.var.get(), FAKE_KEY, "잘라내기로 값이 빠져나감")
        d.cancel()
        self.assertEqual(d.var.get(), "")

    def test_paste_button_rejects_garbage_without_echo(self):
        d = a.SessionKeyDialog(self.root)
        self.root.clipboard_clear(); self.root.clipboard_append("hello world"); self.root.update()
        d.paste()
        self.assertEqual(d.var.get(), "")
        self.assertNotIn("hello", d.status.cget("text"))
        self.root.clipboard_clear(); self.root.clipboard_append(f"sessionKey={FAKE_KEY}; x=1"); self.root.update()
        d.paste()
        self.assertEqual(d.var.get(), FAKE_KEY)
        self.assertNotIn("sk-ant", d.status.cget("text"))
        d.cancel()


class TestThemes(unittest.TestCase):
    def test_palettes_complete_and_valid(self):
        import re as _re
        d, l = a.THEMES["dark"], a.THEMES["light"]
        self.assertEqual(set(d), set(l))
        for pal in (d, l):
            for v in pal.values():
                self.assertRegex(v, r"^#[0-9a-f]{6}$")
        self.assertIn(a.resolve_theme("light"), ("light",))
        self.assertIn(a.resolve_theme("dark"), ("dark",))
        self.assertIn(a.resolve_theme("auto"), ("light", "dark"))

    def test_theme_toggle_has_no_inline_handlers(self):
        # CSP(script nonce)를 지키려면 onclick 같은 인라인 핸들러가 없어야 함
        for html in (a.HTML, a.WIDGET_HTML):
            self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertIn('data-t="light"', a.HTML)
        self.assertIn(':root[data-theme="light"]', a.HTML)
        self.assertIn(':root[data-theme="light"]', a.WIDGET_HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
