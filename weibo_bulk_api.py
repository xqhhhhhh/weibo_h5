#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

MOBILE_UA_POOL = [
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
]


@dataclass
class Account:
    name: str
    cookie: str
    user_agent: str
    referer: str
    accept_language: str
    qps: float


class RiskControlBlockedError(RuntimeError):
    """请求被平台风控拦截（如 ok/errno=-100）。"""


def build_browser_like_headers(account: "Account") -> Dict[str, str]:
    ua = account.user_agent
    headers: Dict[str, str] = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": account.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": account.referer,
        "Origin": "https://m.weibo.cn",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    # 仅 Chromium UA 附带 client hints，避免 UA 与请求头不一致
    if "Chrome/" in ua:
        headers.update(
            {
                "sec-ch-ua": '"Google Chrome";v="121", "Chromium";v="121", "Not=A?Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
            }
        )
    return headers


def apply_cookie_jar(session: requests.Session, cookie_raw: str) -> None:
    for part in cookie_raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        # 同时写入主域和子域，提升接口与页面请求命中率
        session.cookies.set(name, value, domain=".weibo.cn", path="/")
        session.cookies.set(name, value, domain="m.weibo.cn", path="/")


class AccountClient:
    def __init__(self, account: Account, timeout: float, raw_log_path: Optional[Path] = None) -> None:
        self.account = account
        self.timeout = timeout
        self.raw_log_path = raw_log_path
        self.client = requests.Session()
        self.client.headers.update(build_browser_like_headers(account))
        apply_cookie_jar(self.client, account.cookie)
        self._lock = asyncio.Lock()
        self._next_ts = 0.0

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    @staticmethod
    def _is_risk_blocked(payload: Any) -> bool:
        return isinstance(payload, dict) and ((payload.get("ok") == -100) or (payload.get("errno") == -100))

    def _append_raw_log(self, url: str, status_code: int, payload: Any) -> None:
        if not self.raw_log_path:
            return
        self.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(time.time()),
            "account": self.account.name,
            "url": url,
            "status_code": status_code,
            "payload": payload,
        }
        with self.raw_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    async def get_json(self, url: str) -> Dict[str, Any]:
        async with self._lock:
            now = time.time()
            if self._next_ts > now:
                await asyncio.sleep(self._next_ts - now)
            interval = 1.0 / max(0.01, float(self.account.qps))
            self._next_ts = time.time() + interval

            resp = await asyncio.to_thread(self.client.get, url, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()
            self._append_raw_log(url=url, status_code=resp.status_code, payload=payload)
            if self._is_risk_blocked(payload):
                raise RiskControlBlockedError(f"risk control blocked url={url}")
            if isinstance(payload, dict):
                ok_val = payload.get("ok")
                errno_val = payload.get("errno")
                if isinstance(ok_val, (int, float)) and ok_val < 0:
                    raise RuntimeError(f"api error ok={ok_val} errno={errno_val} url={url}")
                if isinstance(errno_val, int) and errno_val < 0:
                    raise RuntimeError(f"api error ok={ok_val} errno={errno_val} url={url}")
            return payload


def parse_args() -> argparse.Namespace:
    def build_parser(required: bool) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(description="微博关键词接口爬虫（request-only 串行）")
        p.add_argument("--config", default="", help="JSON配置文件路径")
        p.add_argument("--csv", required=required, help="关键词CSV")
        p.add_argument("--accounts", required=required, help="账号配置JSON文件")
        p.add_argument("--output", default="output/weibo_bulk_result.jsonl", help="输出JSONL")
        p.add_argument("--raw-log", default="", help="记录每次接口原始返回的JSONL文件路径")
        p.add_argument("--state-db", default="output/weibo_bulk_state.db", help="断点续跑SQLite")
        p.add_argument("--keyword-column", default="", help="关键词列名")
        p.add_argument("--per-account-qps", type=float, default=2.0, help="单账号QPS")
        p.add_argument("--timeout", type=float, default=20.0)
        p.add_argument("--max-retries", type=int, default=3)
        p.add_argument("--max-media-pages", type=int, default=12)
        p.add_argument("--max-contrib-pages", type=int, default=3)
        p.add_argument("--allow-empty-contrib", action="store_true", help="贡献榜为空也算成功")
        p.add_argument("--limit", type=int, default=0, help="仅调试前N个关键词")
        p.add_argument("--refresh-on-not-found", action="store_true", help="命中 found=false 时暂停并刷新本地 Chrome")
        p.add_argument(
            "--refresh-method",
            choices=["auto", "mac", "windows"],
            default="auto",
            help="刷新方式: auto(按系统选择) / mac(AppleScript) / windows(PowerShell)",
        )
        p.add_argument("--refresh-wait", type=float, default=5.0, help="验证成功后等待秒数")
        p.add_argument("--verify-poll-interval", type=float, default=2.0, help="验证码状态轮询间隔秒数")
        p.add_argument("--verify-cycle-timeout", type=float, default=45.0, help="单轮验证等待超时秒数，超时后自动再次刷新")
        p.add_argument("--refresh-url-keyword", default="weibo", help="刷新标签页 URL 需包含的关键词")
        p.add_argument("--refresh-window-keyword", default="Chrome", help="Windows 激活窗口标题关键词")
        return p

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    pre_args, _ = pre.parse_known_args()

    parser = build_parser(required=False)
    if str(pre_args.config).strip():
        config_path = Path(pre_args.config).expanduser()
        if not config_path.exists():
            parser.error(f"配置文件不存在: {config_path}")
        try:
            raw_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            parser.error(f"配置文件解析失败: {e}")
        if not isinstance(raw_cfg, dict):
            parser.error("配置文件必须是 JSON 对象")

        valid_dests = {a.dest for a in parser._actions}
        cfg: Dict[str, Any] = {}
        for k, v in raw_cfg.items():
            if k not in valid_dests:
                parser.error(f"配置文件包含未知字段: {k}")
            cfg[k] = v
        parser.set_defaults(**cfg)

    args = parser.parse_args()
    if not str(args.csv).strip() or not str(args.accounts).strip():
        parser.error("--csv 和 --accounts 必填（可通过 --config 提供）")
    return args


def detect_keyword_column(headers: List[str]) -> str:
    if not headers:
        return ""
    m = {h.strip().lower(): h for h in headers}
    for k in ["keyword", "keywords", "关键词", "关键字", "query", "topic", "话题"]:
        if k in m:
            return m[k]
    return headers[0]


def load_keywords(csv_path: Path, keyword_column: str, limit: int) -> List[str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            col = keyword_column.strip() or detect_keyword_column(reader.fieldnames)
            if col not in reader.fieldnames:
                raise ValueError(f"CSV列不存在: {col}, 可选: {reader.fieldnames}")
            rows = [str((row.get(col) or "")).strip() for row in reader]
        else:
            f.seek(0)
            rows = [str((r[0] if r else "")).strip() for r in csv.reader(f)]

    out: List[str] = []
    seen = set()
    for k in rows:
        if not k or k in seen:
            continue
        out.append(k)
        seen.add(k)
        if limit > 0 and len(out) >= limit:
            break
    return out


def load_accounts(path: Path, per_account_qps: float) -> List[Account]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("accounts JSON 必须是非空数组")

    out: List[Account] = []
    for i, x in enumerate(raw, start=1):
        if not isinstance(x, dict):
            continue
        cookie = str(x.get("cookie") or "").strip()
        if not cookie:
            continue
        out.append(
            Account(
                name=str(x.get("name") or f"acc{i}"),
                cookie=cookie,
                user_agent=str(x.get("user_agent") or MOBILE_UA_POOL[0]),
                referer=str(x.get("referer") or "https://m.weibo.cn/"),
                accept_language=str(x.get("accept_language") or "zh-CN,zh;q=0.9,en;q=0.8"),
                qps=float(x.get("qps") or per_account_qps),
            )
        )
    if not out:
        raise ValueError("accounts JSON 没有有效 cookie")
    return out


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000;")
        # Use rollback journal mode to avoid generating *.db-wal / *.db-shm side files.
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_state (
              keyword TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              retries INTEGER NOT NULL DEFAULT 0,
              error TEXT DEFAULT '',
              updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS result_store (
              keyword TEXT PRIMARY KEY,
              payload TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        return conn
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg:
            raise RuntimeError(
                f"数据库被占用: {db_path}. "
                "请先停止其他正在运行的爬虫进程后重试。"
            ) from e
        raise


def get_done_keywords(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT keyword FROM task_state WHERE status='success'").fetchall()
    return {r[0] for r in rows}


def persist_result(conn: sqlite3.Connection, output: Path, item: Dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    kw = item["keyword"]
    now = int(time.time())
    status = "success" if item.get("_ok") else "failed"
    public_item = {k: v for k, v in item.items() if not k.startswith("_")}

    conn.execute(
        """
        INSERT INTO result_store(keyword, payload, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(keyword) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
        """,
        (kw, json.dumps(public_item, ensure_ascii=False), now),
    )
    conn.execute(
        """
        INSERT INTO task_state(keyword, status, retries, error, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(keyword) DO UPDATE SET status=excluded.status, retries=excluded.retries, error=excluded.error, updated_at=excluded.updated_at
        """,
        (kw, status, 0, item.get("_error", ""), now),
    )
    conn.commit()

    with output.open("a", encoding="utf-8") as f:
        f.write(json.dumps(public_item, ensure_ascii=False) + "\n")


def refresh_chrome_tab_mac(url_keyword: str) -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac refresh requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    repeat with wi from 1 to (count of windows)
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if u contains "{safe_url_keyword}" then
                set active tab index of w to (ti as integer)
                set index of w to 1
                tell t to reload
                activate
                return "OK"
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''
    try:
        ret = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"osascript failed: {type(e).__name__}: {e}"
    out = (ret.stdout or "").strip()
    err = (ret.stderr or "").strip()
    if ret.returncode == 0 and out == "OK":
        return True, "OK"
    reason = out or err or f"returncode={ret.returncode}"
    return False, reason


def refresh_chrome_tab_windows(window_keyword: str) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, "windows refresh requires win32"
    safe_window_keyword = str(window_keyword).replace("'", "''")
    ps = (
        "$ws=New-Object -ComObject WScript.Shell; "
        f"if(-not $ws.AppActivate('{safe_window_keyword}'))"
        "{ Write-Output 'NO_WINDOW'; exit 2 }; "
        "Start-Sleep -Milliseconds 200; "
        "$ws.SendKeys('^r'); "
        "Write-Output 'OK';"
    )
    try:
        ret = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"powershell failed: {type(e).__name__}: {e}"
    out = (ret.stdout or "").strip()
    err = (ret.stderr or "").strip()
    if ret.returncode == 0 and "OK" in out:
        return True, "OK"
    reason = out or err or f"returncode={ret.returncode}"
    return False, reason


def refresh_local_chrome_tab(method: str, url_keyword: str, window_keyword: str) -> Tuple[bool, str]:
    refresh_method = str(method or "auto").strip().lower()
    if refresh_method == "auto":
        if sys.platform == "darwin":
            return refresh_chrome_tab_mac(url_keyword)
        if sys.platform == "win32":
            return refresh_chrome_tab_windows(window_keyword)
        return False, f"auto unsupported platform={sys.platform}"
    if refresh_method == "mac":
        return refresh_chrome_tab_mac(url_keyword)
    if refresh_method == "windows":
        return refresh_chrome_tab_windows(window_keyword)
    return False, f"unknown refresh method={refresh_method}"


def get_chrome_verify_state_mac(url_keyword: str) -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac verify check requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    js = (
        "(() => {"
        "  try {"
        "    const hasCaptcha = !!document.querySelector("
        "      '.yidun_modal,.yidun,.yidun_panel,.yidun_control,.yidun_bgimg'"
        "    );"
        "    if (hasCaptcha) {"
        "      window.dispatchEvent(new Event('codex-run-captcha'));"
        "      return 'PENDING';"
        "    }"
        "    return 'OK';"
        "  } catch (e) {"
        "    return 'JSERR:' + String(e);"
        "  }"
        "})();"
    )
    safe_js = js.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    repeat with wi from 1 to (count of windows)
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if u contains "{safe_url_keyword}" then
                set active tab index of w to (ti as integer)
                set index of w to 1
                set r to execute t javascript "{safe_js}"
                if r is missing value then return "PENDING"
                return r as text
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''
    try:
        ret = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"osascript failed: {type(e).__name__}: {e}"
    out = (ret.stdout or "").strip()
    err = (ret.stderr or "").strip()
    if ret.returncode != 0:
        return False, out or err or f"returncode={ret.returncode}"
    return True, out or "PENDING"


def get_chrome_tab_url_mac(url_keyword: str) -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac url check requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    repeat with wi from 1 to (count of windows)
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if u contains "{safe_url_keyword}" then
                set active tab index of w to (ti as integer)
                set index of w to 1
                return u as text
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''
    try:
        ret = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"osascript failed: {type(e).__name__}: {e}"
    out = (ret.stdout or "").strip()
    err = (ret.stderr or "").strip()
    if ret.returncode != 0:
        return False, out or err or f"returncode={ret.returncode}"
    return True, out


async def handle_not_found_gate(args: argparse.Namespace, keyword: str) -> None:
    refresh_method = str(args.refresh_method or "auto").strip().lower()
    supports_verify_poll = (refresh_method == "mac") or (refresh_method == "auto" and sys.platform == "darwin")

    if not supports_verify_poll:
        print(f"[WARN] keyword={keyword} found=false, refresh Chrome then pause {args.refresh_wait:.1f}s")
        ok, msg = await asyncio.to_thread(
            refresh_local_chrome_tab,
            str(args.refresh_method),
            str(args.refresh_url_keyword),
            str(args.refresh_window_keyword),
        )
        if ok:
            print(
                "[INFO] Chrome refresh success "
                f"method={args.refresh_method} url_keyword={args.refresh_url_keyword} "
                f"window_keyword={args.refresh_window_keyword}"
            )
        else:
            print(f"[WARN] Chrome refresh skipped/failed: {msg}")
        await asyncio.sleep(max(0.0, float(args.refresh_wait)))
        return

    cycle = 0
    poll_interval = max(0.5, float(args.verify_poll_interval))
    cycle_timeout = max(5.0, float(args.verify_cycle_timeout))
    js_disabled_warned = False
    while True:
        cycle += 1
        print(f"[WARN] keyword={keyword} found=false, verify gate cycle={cycle}: refresh and wait for plugin success")
        ok, msg = await asyncio.to_thread(
            refresh_local_chrome_tab,
            str(args.refresh_method),
            str(args.refresh_url_keyword),
            str(args.refresh_window_keyword),
        )
        if not ok:
            print(f"[WARN] Chrome refresh skipped/failed: {msg}")
            await asyncio.sleep(poll_interval)
            continue

        print(
            "[INFO] Chrome refresh success "
            f"method={args.refresh_method} url_keyword={args.refresh_url_keyword} "
            f"window_keyword={args.refresh_window_keyword}"
        )

        begin = time.time()
        while True:
            state_ok, state = await asyncio.to_thread(get_chrome_verify_state_mac, str(args.refresh_url_keyword))
            if not state_ok:
                s = str(state or "")
                if ("允许 Apple 事件中的 JavaScript" in s) or ("Apple events JavaScript" in s):
                    if not js_disabled_warned:
                        print(
                            "[WARN] Chrome 关闭了 AppleScript 执行 JS，"
                            "已降级为 URL 状态检测（离开 /captcha/show 视为验证成功）。"
                        )
                        js_disabled_warned = True
                    url_ok, tab_url = await asyncio.to_thread(get_chrome_tab_url_mac, str(args.refresh_url_keyword))
                    if url_ok:
                        if "/captcha/show" not in str(tab_url):
                            print(f"[INFO] verify success detected by URL, pause {args.refresh_wait:.1f}s then resume crawling")
                            await asyncio.sleep(max(0.0, float(args.refresh_wait)))
                            return
                        elapsed = time.time() - begin
                        if elapsed >= cycle_timeout:
                            print(f"[WARN] verify not finished in {cycle_timeout:.1f}s, reloading captcha page")
                            break
                        await asyncio.sleep(poll_interval)
                        continue
                    print(f"[WARN] verify URL check failed: {tab_url}")
                    break
                print(f"[WARN] verify state check failed: {state}")
                break

            norm = str(state or "").strip().upper()
            if norm == "OK":
                print(f"[INFO] verify success detected, pause {args.refresh_wait:.1f}s then resume crawling")
                await asyncio.sleep(max(0.0, float(args.refresh_wait)))
                return

            if norm.startswith("JSERR"):
                print(f"[WARN] verify state js error: {state}")

            elapsed = time.time() - begin
            if elapsed >= cycle_timeout:
                print(f"[WARN] verify not finished in {cycle_timeout:.1f}s, reloading captcha page")
                break
            await asyncio.sleep(poll_interval)


def encode_query(raw_query: str) -> str:
    return quote(raw_query, safe="")


def build_media_api(raw_query: str, page: int) -> str:
    q = encode_query(raw_query)
    containerid = quote(f"100103type=164&q={q}&t=3", safe="")
    return f"https://m.weibo.cn/api/container/getIndex?containerid={containerid}&page={page}"


def build_contrib_api(raw_query: str, page: int) -> str:
    q = encode_query(raw_query)
    containerid = quote(f"231522type=103&q={q}", safe="")
    return f"https://m.weibo.cn/api/container/getIndex?containerid={containerid}&page={page}"


def build_query_variants(keyword: str) -> List[str]:
    out: List[str] = []
    for q in (f"#{keyword}#", keyword):
        q = q.strip()
        if q and q not in out:
            out.append(q)
    return out


def parse_card_group_users(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    seen = set()
    for card in cards:
        if not isinstance(card, dict):
            continue
        groups = card.get("card_group") if isinstance(card.get("card_group"), list) else []
        for g in groups:
            if not isinstance(g, dict):
                continue
            if g.get("card_type") not in (10, 11) and not isinstance(g.get("user"), dict):
                continue
            user = g.get("user") if isinstance(g.get("user"), dict) else {}
            uid = str(user.get("id") or "")
            if not uid and isinstance(g.get("itemid"), str):
                m = re.search(r"uid=(\d+)", g["itemid"])
                uid = m.group(1) if m else ""
            if not uid or uid in seen:
                continue
            seen.add(uid)
            users.append(
                {
                    "uid": uid,
                    "screen_name": str(user.get("screen_name") or ""),
                    "desc1": str(g.get("desc1") or ""),
                }
            )
    return users


async def fetch_media_list(cli: AccountClient, raw_query: str, max_pages: int) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    all_users: List[Dict[str, Any]] = []
    seen = set()
    total = None
    for p in range(1, max_pages + 1):
        payload = await cli.get_json(build_media_api(raw_query, p))
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            break
        info = data.get("cardlistInfo") if isinstance(data.get("cardlistInfo"), dict) else {}
        if isinstance(info.get("total"), int):
            total = int(info["total"])
        cards = data.get("cards") if isinstance(data.get("cards"), list) else []
        users = parse_card_group_users(cards)

        got = 0
        for u in users:
            uid = str(u.get("uid") or "")
            if uid in seen:
                continue
            seen.add(uid)
            all_users.append({"uid": uid, "screen_name": str(u.get("screen_name") or "")})
            got += 1
        if got == 0:
            break
    return all_users, total


async def fetch_contributors(cli: AccountClient, raw_query: str, max_pages: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in range(1, max_pages + 1):
        payload = await cli.get_json(build_contrib_api(raw_query, p))
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            break
        cards = data.get("cards") if isinstance(data.get("cards"), list) else []
        users = parse_card_group_users(cards)

        got = 0
        for u in users:
            uid = str(u.get("uid") or "")
            if uid in seen:
                continue
            seen.add(uid)
            desc1 = str(u.get("desc1") or "")
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", desc1)
            val: Any = None
            if m:
                try:
                    f = float(m.group(1))
                    val = int(f) if f.is_integer() else f
                except Exception:
                    val = m.group(1)
            out.append(
                {
                    "rank": len(out) + 1,
                    "uid": uid,
                    "name": str(u.get("screen_name") or ""),
                    "contribution_value": val,
                }
            )
            got += 1
        if got == 0:
            break
    return out


async def process_keyword(keyword: str, clients: List[AccountClient], args: argparse.Namespace) -> Dict[str, Any]:
    idx = int(hashlib.md5(keyword.encode("utf-8")).hexdigest(), 16) % len(clients)
    order = [clients[(idx + i) % len(clients)] for i in range(len(clients))]
    last_err = ""

    for attempt in range(1, args.max_retries + 1):
        for cli in order:
            try:
                variants = build_query_variants(keyword)
                for i, raw_query in enumerate(variants):
                    is_last_variant = i == len(variants) - 1
                    try:
                        media_list, media_total = await fetch_media_list(cli, raw_query, args.max_media_pages)
                        contrib_list = await fetch_contributors(cli, raw_query, args.max_contrib_pages)
                    except Exception:
                        if not is_last_variant:
                            continue
                        raise

                    host = str(media_list[0].get("screen_name") or "") if media_list else ""
                    found = bool(media_list or contrib_list)
                    if (not contrib_list) and (not args.allow_empty_contrib):
                        if not is_last_variant:
                            continue
                        raise RuntimeError("contributors empty")

                    return {
                        "keyword": keyword,
                        "found": found,
                        "media_publish_count": media_total if isinstance(media_total, int) else None,
                        "host": host,
                        "publish_media_list": media_list,
                        "top_contributors": contrib_list,
                        "_ok": True,
                        "_error": "",
                    }
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(min(8.0, 0.6 * attempt + random.random()))

    return {
        "keyword": keyword,
        "found": False,
        "media_publish_count": None,
        "host": "",
        "publish_media_list": [],
        "top_contributors": [],
        "_ok": False,
        "_error": last_err or "unknown",
    }


async def main_async(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    accounts_path = Path(args.accounts)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV不存在: {csv_path}")
    if not accounts_path.exists():
        raise FileNotFoundError(f"账号文件不存在: {accounts_path}")

    keywords = load_keywords(csv_path, args.keyword_column, args.limit)
    accounts = load_accounts(accounts_path, args.per_account_qps)
    raw_log_path = Path(args.raw_log).expanduser() if str(args.raw_log).strip() else None
    clients = [AccountClient(a, timeout=args.timeout, raw_log_path=raw_log_path) for a in accounts]

    conn = init_db(Path(args.state_db))
    done = get_done_keywords(conn)
    todo = [k for k in keywords if k not in done]

    print(f"[INFO] keywords={len(keywords)} done={len(done)} todo={len(todo)} accounts={len(accounts)} mode=serial")

    output_path = Path(args.output)
    try:
        for kw in todo:
            result = await process_keyword(kw, clients, args)
            persist_result(conn, output_path, result)
            if args.refresh_on_not_found and (result.get("found") is False):
                await handle_not_found_gate(args, kw)
    finally:
        for c in clients:
            await c.close()
        conn.close()

    print("[DONE] finished")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
