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
from typing import Any, Callable, Dict, List, Optional, Tuple
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
    refresh_method: str
    refresh_url_keyword: str
    refresh_window_keyword: str
    refresh_window_index: int
    refresh_window_tag: str


@dataclass(frozen=True)
class ApiEndpoint:
    name: str
    containerid_template: str
    parser: str
    output_field: str
    max_pages: int
    total_field: str = ""


class RiskControlBlockedError(RuntimeError):
    """请求被平台风控拦截（如 ok/errno=-100）。"""


BUILTIN_API_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "media": {
        "containerid_template": "100103type=164&q={q}&t=3",
        "parser": "users_basic",
        "output_field": "publish_media_list",
        "total_field": "media_publish_count",
    },
    "contributors": {
        "containerid_template": "231522type=103&q={q}",
        "parser": "contributors",
        "output_field": "top_contributors",
        "total_field": "",
    },
}

SUPPORTED_ENDPOINT_PARSERS = {"users_basic", "contributors", "raw_cards"}


def _platform_from_ua(ua: str) -> str:
    s = str(ua or "")
    if "Android" in s:
        return "Android"
    if "iPhone" in s or "iPad" in s:
        return "iOS"
    if "Mac OS X" in s:
        return "macOS"
    if "Windows" in s:
        return "Windows"
    return "Linux"


def _is_mobile_ua(ua: str) -> bool:
    s = str(ua or "")
    return ("Mobile" in s) or ("Android" in s) or ("iPhone" in s) or ("iPad" in s)


def _extract_chrome_version(ua: str) -> Tuple[str, str]:
    m = re.search(r"Chrome/(\d+)(\.[0-9.]+)?", str(ua or ""))
    if not m:
        return "121", "121.0.0.0"
    major = str(m.group(1))
    full = f"{major}{str(m.group(2) or '.0.0.0')}"
    return major, full


def _is_chromium_ua(ua: str) -> bool:
    s = str(ua or "")
    return ("Chrome/" in s) and ("Edg/" not in s) and ("OPR/" not in s) and ("CriOS/" not in s)


def _build_chromium_client_hints(ua: str) -> Dict[str, str]:
    major, _ = _extract_chrome_version(ua)
    platform = _platform_from_ua(ua)
    mobile = "?1" if _is_mobile_ua(ua) else "?0"
    return {
        "sec-ch-ua": f'"Not.A/Brand";v="99", "Chromium";v="{major}", "Google Chrome";v="{major}"',
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": f'"{platform}"',
    }


def _origin_from_referer(referer: str) -> str:
    s = str(referer or "").strip()
    m = re.match(r"^(https?://[^/]+)", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return "https://m.weibo.cn"


def build_browser_like_headers(account: "Account") -> Dict[str, str]:
    ua = account.user_agent
    headers: Dict[str, str] = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": account.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": account.referer,
        "Origin": _origin_from_referer(account.referer),
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    # 仅 Chromium UA 附带 client hints，避免 UA 与请求头不一致（Safari 不带 sec-ch-*）
    if _is_chromium_ua(ua):
        headers.update(_build_chromium_client_hints(ua))
        headers["Priority"] = "u=1, i"
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
        p.add_argument("--shard-index", type=int, default=-1, help="关键词分片索引（从0开始，-1表示不分片）")
        p.add_argument("--shard-total", type=int, default=1, help="关键词分片总数")
        p.add_argument("--per-account-qps", type=float, default=2.0, help="单账号QPS")
        p.add_argument("--timeout", type=float, default=20.0)
        p.add_argument("--max-retries", type=int, default=3)
        p.add_argument("--max-media-pages", type=int, default=12)
        p.add_argument("--max-contrib-pages", type=int, default=3)
        p.add_argument("--allow-empty-contrib", action="store_true", help="贡献榜为空也算成功")
        p.add_argument(
            "--api-endpoints",
            default=None,
            help=(
                "接口抓取配置。支持逗号分隔的内置名称(如 media,contributors)，"
                "或 JSON 数组（字符串或对象）"
            ),
        )
        p.add_argument(
            "--query-templates",
            default=None,
            help="关键词变体模板。支持逗号分隔或 JSON 数组，模板中可使用 {keyword}",
        )
        p.add_argument("--limit", type=int, default=0, help="仅调试前N个关键词")
        p.add_argument("--refresh-on-not-found", action="store_true", help="命中 found=false 时暂停并刷新本地 Chrome")
        p.add_argument(
            "--retry-false-after-verify",
            dest="retry_false_after_verify",
            action="store_true",
            default=True,
            help="命中 found=false 并完成验证闸门后，立即重抓该关键词一次",
        )
        p.add_argument("--no-retry-false-after-verify", dest="retry_false_after_verify", action="store_false", help=argparse.SUPPRESS)
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
        p.add_argument("--refresh-window-index", type=int, default=0, help="mac下绑定 Chrome 窗口序号（从1开始，0表示不指定）")
        p.add_argument("--refresh-window-tag", default="", help="mac下绑定窗口标签（window.name）")
        p.add_argument("--concurrency", type=int, default=1, help="并发 worker 数（建议不超过账号数）")
        p.add_argument("--strict-account-isolation", action="store_true", help="关键词固定单账号处理，不跨账号回退")
        p.add_argument(
            "--fallback-to-other-accounts",
            dest="fallback_to_other_accounts",
            action="store_true",
            default=True,
            help="主账号失败时回退到其他账号重试",
        )
        p.add_argument("--no-fallback-to-other-accounts", dest="fallback_to_other_accounts", action="store_false", help=argparse.SUPPRESS)
        # 允许 run_config 与 captcha_server 共用，主爬虫不直接消费这些字段。
        p.add_argument("--captcha-host", default="127.0.0.1", help=argparse.SUPPRESS)
        p.add_argument("--captcha-port", type=int, default=5050, help=argparse.SUPPRESS)
        p.add_argument("--captcha-timeout", type=float, default=15.0, help=argparse.SUPPRESS)
        p.add_argument("--captcha-x-offset", type=int, default=0, help=argparse.SUPPRESS)
        p.add_argument("--captcha-distance-offset-px", type=int, default=0, help=argparse.SUPPRESS)
        p.add_argument("--captcha-low-confidence-threshold", type=float, default=0.62, help=argparse.SUPPRESS)
        p.add_argument("--captcha-consistency-tolerance", type=int, default=5, help=argparse.SUPPRESS)
        p.add_argument("--captcha-debug", action="store_true", help=argparse.SUPPRESS)
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
    if int(args.shard_total) <= 0:
        parser.error("--shard-total 必须 > 0")
    if int(args.shard_index) >= 0 and int(args.shard_index) >= int(args.shard_total):
        parser.error("--shard-index 必须在 [0, shard_total) 范围内")
    return args


def detect_keyword_column(headers: List[str]) -> str:
    if not headers:
        return ""
    m = {h.strip().lower(): h for h in headers}
    for k in ["keyword", "keywords", "关键词", "关键字", "query", "topic", "话题"]:
        if k in m:
            return m[k]
    return headers[0]


def load_keywords(csv_path: Path, keyword_column: str, limit: int, shard_index: int, shard_total: int) -> List[str]:
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

    out_all: List[str] = []
    seen = set()
    for k in rows:
        if not k or k in seen:
            continue
        out_all.append(k)
        seen.add(k)
    if shard_index >= 0:
        out = [k for k in out_all if (int(hashlib.md5(k.encode("utf-8")).hexdigest(), 16) % shard_total) == shard_index]
    else:
        out = out_all
    if limit > 0:
        out = out[:limit]
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
                refresh_method=str(x.get("refresh_method") or "").strip().lower(),
                refresh_url_keyword=str(x.get("refresh_url_keyword") or "").strip(),
                refresh_window_keyword=str(x.get("refresh_window_keyword") or "").strip(),
                refresh_window_index=int(x.get("refresh_window_index") or 0),
                refresh_window_tag=str(x.get("refresh_window_tag") or "").strip(),
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
    status = "success" if item.get("found") is True else "failed"
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


def refresh_chrome_tab_mac(url_keyword: str, window_index: int = 0, window_tag: str = "") -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac refresh requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    safe_window_index = int(window_index or 0)
    safe_window_tag = str(window_tag or "").replace("\\", "\\\\").replace('"', '\\"')
    use_window_tag = bool(safe_window_tag)
    use_window_index = safe_window_index > 0
    url_check = "true" if not safe_url_keyword else f'(u contains "{safe_url_keyword}")'
    tag_js = "(() => { try { return window.name || ''; } catch (e) { return ''; } })();"
    safe_tag_js = tag_js.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    set winStart to 1
    set winEnd to (count of windows)
    if {"true" if use_window_index else "false"} then
        if ({safe_window_index} > winEnd) then return "NO_WINDOW_INDEX"
        set winStart to {safe_window_index}
        set winEnd to {safe_window_index}
    end if
    repeat with wi from winStart to winEnd
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if {"true" if use_window_tag else "false"} then
                set n to execute t javascript "{safe_tag_js}"
                if n is missing value then set n to ""
                if (n as text) is "{safe_window_tag}" then
                    tell t to reload
                    return "OK"
                end if
            else
                if {url_check} then
                    tell t to reload
                    return "OK"
                end if
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


def refresh_local_chrome_tab(
    method: str,
    url_keyword: str,
    window_keyword: str,
    window_index: int = 0,
    window_tag: str = "",
) -> Tuple[bool, str]:
    refresh_method = str(method or "auto").strip().lower()
    if refresh_method == "auto":
        if sys.platform == "darwin":
            return refresh_chrome_tab_mac(url_keyword, window_index=window_index, window_tag=window_tag)
        if sys.platform == "win32":
            return refresh_chrome_tab_windows(window_keyword)
        return False, f"auto unsupported platform={sys.platform}"
    if refresh_method == "mac":
        return refresh_chrome_tab_mac(url_keyword, window_index=window_index, window_tag=window_tag)
    if refresh_method == "windows":
        return refresh_chrome_tab_windows(window_keyword)
    return False, f"unknown refresh method={refresh_method}"


def get_chrome_verify_state_mac(url_keyword: str, window_index: int = 0, window_tag: str = "") -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac verify check requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    safe_window_index = int(window_index or 0)
    safe_window_tag = str(window_tag or "").replace("\\", "\\\\").replace('"', '\\"')
    use_window_tag = bool(safe_window_tag)
    use_window_index = safe_window_index > 0
    url_check = "true" if not safe_url_keyword else f'(u contains "{safe_url_keyword}")'
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
    tag_js = "(() => { try { return window.name || ''; } catch (e) { return ''; } })();"
    safe_tag_js = tag_js.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    set winStart to 1
    set winEnd to (count of windows)
    if {"true" if use_window_index else "false"} then
        if ({safe_window_index} > winEnd) then return "NO_WINDOW_INDEX"
        set winStart to {safe_window_index}
        set winEnd to {safe_window_index}
    end if
    repeat with wi from winStart to winEnd
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if {"true" if use_window_tag else "false"} then
                set n to execute t javascript "{safe_tag_js}"
                if n is missing value then set n to ""
                if (n as text) is "{safe_window_tag}" then
                    set r to execute t javascript "{safe_js}"
                    if r is missing value then return "PENDING"
                    return r as text
                end if
            else
                if {url_check} then
                    set r to execute t javascript "{safe_js}"
                    if r is missing value then return "PENDING"
                    return r as text
                end if
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


def get_chrome_tab_url_mac(url_keyword: str, window_index: int = 0, window_tag: str = "") -> Tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "mac url check requires macOS"
    safe_url_keyword = str(url_keyword).replace("\\", "\\\\").replace('"', '\\"')
    safe_window_index = int(window_index or 0)
    safe_window_tag = str(window_tag or "").replace("\\", "\\\\").replace('"', '\\"')
    use_window_tag = bool(safe_window_tag)
    use_window_index = safe_window_index > 0
    url_check = "true" if not safe_url_keyword else f'(u contains "{safe_url_keyword}")'
    tag_js = "(() => { try { return window.name || ''; } catch (e) { return ''; } })();"
    safe_tag_js = tag_js.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
    if (count of windows) is 0 then return "NO_WINDOW"
    set winStart to 1
    set winEnd to (count of windows)
    if {"true" if use_window_index else "false"} then
        if ({safe_window_index} > winEnd) then return "NO_WINDOW_INDEX"
        set winStart to {safe_window_index}
        set winEnd to {safe_window_index}
    end if
    repeat with wi from winStart to winEnd
        set w to window wi
        repeat with ti from 1 to (count of tabs of w)
            set t to tab (ti as integer) of w
            set u to URL of t
            if {"true" if use_window_tag else "false"} then
                set n to execute t javascript "{safe_tag_js}"
                if n is missing value then set n to ""
                if (n as text) is "{safe_window_tag}" then return u as text
            else
                if {url_check} then
                    return u as text
                end if
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


def resolve_account_refresh_settings(args: argparse.Namespace, account: Optional[Account]) -> Tuple[str, str, str, int, str]:
    method = str(args.refresh_method or "auto").strip().lower()
    url_keyword = str(args.refresh_url_keyword or "weibo")
    window_keyword = str(args.refresh_window_keyword or "Chrome")
    window_index = int(args.refresh_window_index or 0)
    window_tag = str(args.refresh_window_tag or "").strip()
    if account:
        if str(account.refresh_method or "").strip():
            method = str(account.refresh_method).strip().lower()
        if str(account.refresh_url_keyword or "").strip():
            url_keyword = str(account.refresh_url_keyword).strip()
        if str(account.refresh_window_keyword or "").strip():
            window_keyword = str(account.refresh_window_keyword).strip()
        if int(account.refresh_window_index or 0) > 0:
            window_index = int(account.refresh_window_index)
        if str(account.refresh_window_tag or "").strip():
            window_tag = str(account.refresh_window_tag).strip()
    return method, url_keyword, window_keyword, window_index, window_tag


async def handle_not_found_gate(
    args: argparse.Namespace,
    keyword: str,
    account: Optional[Account],
    gate_lock: asyncio.Lock,
) -> None:
    refresh_method, refresh_url_keyword, refresh_window_keyword, refresh_window_index, refresh_window_tag = resolve_account_refresh_settings(args, account)
    supports_verify_poll = (refresh_method == "mac") or (refresh_method == "auto" and sys.platform == "darwin")
    acc_name = str(account.name) if account else "unknown"

    async with gate_lock:
        if not supports_verify_poll:
            print(f"[WARN] account={acc_name} keyword={keyword} found=false, refresh Chrome then pause {args.refresh_wait:.1f}s")
            ok, msg = await asyncio.to_thread(
                refresh_local_chrome_tab,
                str(refresh_method),
                str(refresh_url_keyword),
                str(refresh_window_keyword),
                int(refresh_window_index),
                str(refresh_window_tag),
            )
            if ok:
                print(
                    "[INFO] Chrome refresh success "
                    f"account={acc_name} method={refresh_method} url_keyword={refresh_url_keyword} "
                    f"window_keyword={refresh_window_keyword} window_index={refresh_window_index} window_tag={refresh_window_tag or '-'}"
                )
            else:
                print(f"[WARN] Chrome refresh skipped/failed account={acc_name}: {msg}")
            await asyncio.sleep(max(0.0, float(args.refresh_wait)))
            return

        cycle = 0
        poll_interval = max(0.5, float(args.verify_poll_interval))
        cycle_timeout = max(5.0, float(args.verify_cycle_timeout))
        js_disabled_warned = False
        while True:
            cycle += 1
            print(
                f"[WARN] account={acc_name} keyword={keyword} found=false, "
                f"verify gate cycle={cycle}: refresh and wait for plugin success"
            )
            ok, msg = await asyncio.to_thread(
                refresh_local_chrome_tab,
                str(refresh_method),
                str(refresh_url_keyword),
                str(refresh_window_keyword),
                int(refresh_window_index),
                str(refresh_window_tag),
            )
            if not ok:
                print(f"[WARN] Chrome refresh skipped/failed account={acc_name}: {msg}")
                await asyncio.sleep(poll_interval)
                continue

            print(
                "[INFO] Chrome refresh success "
                f"account={acc_name} method={refresh_method} url_keyword={refresh_url_keyword} "
                f"window_keyword={refresh_window_keyword} window_index={refresh_window_index} window_tag={refresh_window_tag or '-'}"
            )

            begin = time.time()
            while True:
                state_ok, state = await asyncio.to_thread(
                    get_chrome_verify_state_mac,
                    str(refresh_url_keyword),
                    int(refresh_window_index),
                    str(refresh_window_tag),
                )
                if not state_ok:
                    s = str(state or "")
                    if ("允许 Apple 事件中的 JavaScript" in s) or ("Apple events JavaScript" in s):
                        if str(refresh_window_tag or "").strip():
                            print(
                                "[WARN] tag绑定模式需要 AppleScript 执行 JS，"
                                f"account={acc_name} window_tag={refresh_window_tag} 无法降级到 URL 检测。"
                            )
                            break
                        if not js_disabled_warned:
                            print(
                                "[WARN] Chrome 关闭了 AppleScript 执行 JS，"
                                "已降级为 URL 状态检测（离开 /captcha/show 视为验证成功）。"
                            )
                            js_disabled_warned = True
                        url_ok, tab_url = await asyncio.to_thread(
                            get_chrome_tab_url_mac,
                            str(refresh_url_keyword),
                            int(refresh_window_index),
                            str(refresh_window_tag),
                        )
                        if url_ok:
                            if "/captcha/show" not in str(tab_url):
                                print(
                                    "[INFO] verify success detected by URL, "
                                    f"account={acc_name}, pause {args.refresh_wait:.1f}s then resume crawling"
                                )
                                await asyncio.sleep(max(0.0, float(args.refresh_wait)))
                                return
                            elapsed = time.time() - begin
                            if elapsed >= cycle_timeout:
                                print(f"[WARN] verify not finished in {cycle_timeout:.1f}s, reloading captcha page")
                                break
                            await asyncio.sleep(poll_interval)
                            continue
                        print(f"[WARN] verify URL check failed account={acc_name}: {tab_url}")
                        break
                    print(f"[WARN] verify state check failed account={acc_name}: {state}")
                    break

                norm = str(state or "").strip().upper()
                if norm == "OK":
                    print(
                        "[INFO] verify success detected, "
                        f"account={acc_name}, pause {args.refresh_wait:.1f}s then resume crawling"
                    )
                    await asyncio.sleep(max(0.0, float(args.refresh_wait)))
                    return

                if norm.startswith("JSERR"):
                    print(f"[WARN] verify state js error account={acc_name}: {state}")

                elapsed = time.time() - begin
                if elapsed >= cycle_timeout:
                    print(f"[WARN] verify not finished in {cycle_timeout:.1f}s, reloading captcha page")
                    break
                await asyncio.sleep(poll_interval)


def encode_query(raw_query: str) -> str:
    return quote(raw_query, safe="")


def _parse_list_config(value: Any, field_name: str) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("[") or s.startswith("{"):
            try:
                parsed = json.loads(s)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"{field_name} JSON 解析失败: {e}") from e
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        return [x.strip() for x in s.split(",") if x.strip()]
    raise ValueError(f"{field_name} 必须是 list/tuple/str，当前={type(value).__name__}")


def _default_max_pages_for_endpoint(name: str, args: argparse.Namespace) -> int:
    if name == "media":
        return int(args.max_media_pages)
    if name == "contributors":
        return int(args.max_contrib_pages)
    return 1


def _normalize_endpoint_from_name(name: str, args: argparse.Namespace) -> ApiEndpoint:
    k = str(name or "").strip().lower()
    base = BUILTIN_API_ENDPOINTS.get(k)
    if not base:
        raise ValueError(f"未知内置 endpoint: {name}")
    parser = str(base["parser"]).strip().lower()
    if parser not in SUPPORTED_ENDPOINT_PARSERS:
        raise ValueError(f"内置 endpoint parser 不支持: {parser}")
    return ApiEndpoint(
        name=k,
        containerid_template=str(base["containerid_template"]),
        parser=parser,
        output_field=str(base["output_field"]),
        max_pages=max(1, _default_max_pages_for_endpoint(k, args)),
        total_field=str(base.get("total_field") or ""),
    )


def _normalize_endpoint_from_dict(entry: Dict[str, Any], args: argparse.Namespace, idx: int) -> ApiEndpoint:
    name = str(entry.get("name") or entry.get("builtin") or "").strip().lower()
    base = BUILTIN_API_ENDPOINTS.get(name, {})
    containerid_template = str(entry.get("containerid_template") or entry.get("containerid") or base.get("containerid_template") or "").strip()
    parser = str(entry.get("parser") or base.get("parser") or "").strip().lower()
    output_field = str(entry.get("output_field") or base.get("output_field") or "").strip()
    if "total_field" in entry:
        total_field = str(entry.get("total_field") or "").strip()
    else:
        total_field = str(base.get("total_field") or "").strip()

    raw_max_pages = entry.get("max_pages")
    if raw_max_pages is None:
        raw_max_pages = _default_max_pages_for_endpoint(name, args)
    max_pages = max(1, int(raw_max_pages))

    if not name:
        name = f"custom_{idx}"
    if not containerid_template:
        raise ValueError(f"endpoint[{idx}] 缺少 containerid_template/containerid")
    if not output_field:
        raise ValueError(f"endpoint[{idx}] 缺少 output_field")
    if not parser:
        raise ValueError(f"endpoint[{idx}] 缺少 parser")
    if parser not in SUPPORTED_ENDPOINT_PARSERS:
        raise ValueError(f"endpoint[{idx}] parser 不支持: {parser}")
    if "{q}" not in containerid_template:
        raise ValueError(f"endpoint[{idx}] containerid_template 必须包含 '{{q}}'")

    return ApiEndpoint(
        name=name,
        containerid_template=containerid_template,
        parser=parser,
        output_field=output_field,
        max_pages=max_pages,
        total_field=total_field,
    )


def resolve_api_endpoints(args: argparse.Namespace) -> List[ApiEndpoint]:
    entries = _parse_list_config(getattr(args, "api_endpoints", None), "api_endpoints")
    if not entries:
        entries = ["media", "contributors"]

    out: List[ApiEndpoint] = []
    output_fields = set()
    for i, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            ep = _normalize_endpoint_from_name(entry, args)
        elif isinstance(entry, dict):
            ep = _normalize_endpoint_from_dict(entry, args, i)
        else:
            raise ValueError(f"api_endpoints[{i}] 必须是 str 或 object")
        if ep.output_field in output_fields:
            raise ValueError(f"api_endpoints output_field 重复: {ep.output_field}")
        output_fields.add(ep.output_field)
        out.append(ep)
    return out


def resolve_query_templates(args: argparse.Namespace) -> List[str]:
    entries = _parse_list_config(getattr(args, "query_templates", None), "query_templates")
    if not entries:
        entries = ["#{keyword}#", "{keyword}"]

    out: List[str] = []
    seen = set()
    for i, x in enumerate(entries, start=1):
        if not isinstance(x, str):
            raise ValueError(f"query_templates[{i}] 必须是字符串")
        t = x.strip()
        if not t:
            continue
        if t not in seen:
            out.append(t)
            seen.add(t)
    if not out:
        raise ValueError("query_templates 不能为空")
    return out


def build_query_variants(keyword: str, templates: List[str]) -> List[str]:
    out: List[str] = []
    for tpl in templates:
        q = tpl.replace("{keyword}", keyword).strip()
        if q and q not in out:
            out.append(q)
    return out


def build_endpoint_api(raw_query: str, page: int, containerid_template: str) -> str:
    q = encode_query(raw_query)
    containerid_raw = containerid_template.format(q=q, query=q, keyword=raw_query)
    containerid = quote(containerid_raw, safe="")
    return f"https://m.weibo.cn/api/container/getIndex?containerid={containerid}&page={page}"


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


def parse_number_from_text(text: str) -> Any:
    s = str(text or "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
    if not m:
        return None
    try:
        f = float(m.group(1))
        return int(f) if f.is_integer() else f
    except Exception:  # noqa: BLE001
        return m.group(1)


def map_user_basic(user: Dict[str, Any], _: int) -> Dict[str, Any]:
    return {
        "uid": str(user.get("uid") or ""),
        "screen_name": str(user.get("screen_name") or ""),
    }


def map_user_contributor(user: Dict[str, Any], rank: int) -> Dict[str, Any]:
    return {
        "rank": rank,
        "uid": str(user.get("uid") or ""),
        "name": str(user.get("screen_name") or ""),
        "contribution_value": parse_number_from_text(str(user.get("desc1") or "")),
    }


USER_MAPPERS: Dict[str, Callable[[Dict[str, Any], int], Dict[str, Any]]] = {
    "users_basic": map_user_basic,
    "contributors": map_user_contributor,
}


async def fetch_endpoint_items(cli: AccountClient, raw_query: str, endpoint: ApiEndpoint) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    all_items: List[Dict[str, Any]] = []
    seen_uids = set()
    total = None
    mapper = USER_MAPPERS.get(endpoint.parser)
    parser_name = endpoint.parser.lower()

    for p in range(1, endpoint.max_pages + 1):
        payload = await cli.get_json(build_endpoint_api(raw_query, p, endpoint.containerid_template))
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            break
        info = data.get("cardlistInfo") if isinstance(data.get("cardlistInfo"), dict) else {}
        if isinstance(info.get("total"), int):
            total = int(info["total"])
        cards = data.get("cards") if isinstance(data.get("cards"), list) else []

        got = 0
        if parser_name == "raw_cards":
            for card in cards:
                if not isinstance(card, dict):
                    continue
                all_items.append(card)
                got += 1
        else:
            if mapper is None:
                raise ValueError(f"endpoint parser 不支持: {endpoint.parser}")
            users = parse_card_group_users(cards)
            for user in users:
                uid = str(user.get("uid") or "")
                if not uid or uid in seen_uids:
                    continue
                seen_uids.add(uid)
                all_items.append(mapper(user, len(all_items) + 1))
                got += 1
        if got == 0:
            break
    return all_items, total


def extract_host(result_payload: Dict[str, Any]) -> str:
    media = result_payload.get("publish_media_list")
    if isinstance(media, list) and media and isinstance(media[0], dict):
        return str(media[0].get("screen_name") or media[0].get("name") or "")
    for v in result_payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            host = str(v[0].get("screen_name") or v[0].get("name") or "")
            if host:
                return host
    return ""


async def process_keyword(
    keyword: str,
    clients: List[AccountClient],
    args: argparse.Namespace,
    endpoints: List[ApiEndpoint],
    query_templates: List[str],
) -> Dict[str, Any]:
    idx = int(hashlib.md5(keyword.encode("utf-8")).hexdigest(), 16) % len(clients)
    order = [clients[(idx + i) % len(clients)] for i in range(len(clients))]
    strict_mode = bool(getattr(args, "strict_account_isolation", False))
    fallback_mode = bool(getattr(args, "fallback_to_other_accounts", True))
    if strict_mode:
        fallback_mode = False
    active_order = order if fallback_mode else [order[0]]
    last_err = ""
    last_account = ""

    for attempt in range(1, args.max_retries + 1):
        for cli in active_order:
            try:
                variants = build_query_variants(keyword, query_templates)
                for i, raw_query in enumerate(variants):
                    is_last_variant = i == len(variants) - 1
                    try:
                        endpoint_payload: Dict[str, Any] = {}
                        for endpoint in endpoints:
                            items, total = await fetch_endpoint_items(cli, raw_query, endpoint)
                            endpoint_payload[endpoint.output_field] = items
                            if endpoint.total_field:
                                endpoint_payload[endpoint.total_field] = total if isinstance(total, int) else None
                    except Exception:
                        if not is_last_variant:
                            continue
                        raise

                    default_payload: Dict[str, Any] = {
                        "media_publish_count": None,
                        "host": "",
                        "publish_media_list": [],
                        "top_contributors": [],
                    }
                    default_payload.update(endpoint_payload)

                    contrib_list = default_payload.get("top_contributors")
                    if contrib_list is None:
                        contrib_list = []
                    has_contrib_endpoint = any(ep.output_field == "top_contributors" for ep in endpoints)
                    found = any(bool(default_payload.get(endpoint.output_field)) for endpoint in endpoints)
                    if has_contrib_endpoint and (not contrib_list) and (not args.allow_empty_contrib):
                        if not is_last_variant:
                            continue
                        raise RuntimeError("contributors empty")
                    default_payload["host"] = extract_host(default_payload)

                    return {
                        "keyword": keyword,
                        "found": found,
                        **default_payload,
                        "_account": cli.account.name,
                        "_ok": True,
                        "_error": "",
                    }
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                last_account = cli.account.name
                await asyncio.sleep(min(8.0, 0.6 * attempt + random.random()))

    return {
        "keyword": keyword,
        "found": False,
        "media_publish_count": None,
        "host": "",
        "publish_media_list": [],
        "top_contributors": [],
        "_account": last_account,
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

    keywords = load_keywords(
        csv_path,
        args.keyword_column,
        args.limit,
        int(args.shard_index),
        int(args.shard_total),
    )
    api_endpoints = resolve_api_endpoints(args)
    query_templates = resolve_query_templates(args)
    accounts = load_accounts(accounts_path, args.per_account_qps)
    raw_log_path = Path(args.raw_log).expanduser() if str(args.raw_log).strip() else None
    clients = [AccountClient(a, timeout=args.timeout, raw_log_path=raw_log_path) for a in accounts]
    account_by_name: Dict[str, Account] = {a.name: a for a in accounts}

    conn = init_db(Path(args.state_db))
    worker_count = max(1, int(args.concurrency))
    if worker_count > len(accounts):
        worker_count = len(accounts)
    mode = "serial" if worker_count <= 1 else f"parallel({worker_count})"
    strict_mode = bool(getattr(args, "strict_account_isolation", False))
    fallback_mode = bool(getattr(args, "fallback_to_other_accounts", True))
    if strict_mode:
        fallback_mode = False
    shard_desc = "all" if int(args.shard_index) < 0 else f"{int(args.shard_index)}/{int(args.shard_total)}"
    initial_done = get_done_keywords(conn)
    print(
        "[INFO] "
        f"keywords={len(keywords)} done={len(initial_done)} todo={len(keywords) - len(initial_done)} accounts={len(accounts)} "
        f"mode={mode} shard={shard_desc} "
        f"strict_account_isolation={strict_mode} fallback_to_other_accounts={fallback_mode} "
        f"endpoints={[e.name for e in api_endpoints]} query_templates={query_templates}"
    )

    output_path = Path(args.output)
    persist_lock = asyncio.Lock()
    gate_locks: Dict[str, asyncio.Lock] = {}

    def gate_key_for_account(acc: Optional[Account]) -> str:
        if not acc:
            return "global"
        method, url_kw, win_kw, win_idx, win_tag = resolve_account_refresh_settings(args, acc)
        return f"{method}|{url_kw}|{win_kw}|{win_idx}|{win_tag}"

    def gate_lock_for_account(acc: Optional[Account]) -> asyncio.Lock:
        key = gate_key_for_account(acc)
        lk = gate_locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            gate_locks[key] = lk
        return lk

    async def run_one_keyword(kw: str) -> None:
        result = await process_keyword(kw, clients, args, api_endpoints, query_templates)
        if args.refresh_on_not_found and (result.get("found") is False):
            acc_name = str(result.get("_account") or "")
            acc_obj = account_by_name.get(acc_name) if acc_name else None
            await handle_not_found_gate(args, kw, acc_obj, gate_lock_for_account(acc_obj))
            if bool(getattr(args, "retry_false_after_verify", True)):
                print(f"[INFO] account={acc_name or '-'} keyword={kw} retry once after verify gate")
                retry_result = await process_keyword(kw, clients, args, api_endpoints, query_templates)
                # 二次结果作为最终写入结果（无论是否变为 true）。
                result = retry_result
        async with persist_lock:
            persist_result(conn, output_path, result)

    try:
        round_idx = 0
        while True:
            done = get_done_keywords(conn)
            todo = [k for k in keywords if k not in done]
            if not todo:
                break

            round_idx += 1
            print(f"[INFO] round={round_idx} start done={len(done)} remaining={len(todo)}")
            before_done_count = len(done)

            if worker_count <= 1:
                for kw in todo:
                    await run_one_keyword(kw)
            else:
                queue: asyncio.Queue[str] = asyncio.Queue()
                for kw in todo:
                    queue.put_nowait(kw)

                async def worker(worker_id: int) -> None:
                    while True:
                        try:
                            kw = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        try:
                            await run_one_keyword(kw)
                        except Exception as e:  # noqa: BLE001
                            print(f"[ERROR] worker={worker_id} keyword={kw} error={type(e).__name__}: {e}")
                        finally:
                            queue.task_done()

                tasks = [asyncio.create_task(worker(i + 1)) for i in range(worker_count)]
                await queue.join()
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            after_done_count = len(get_done_keywords(conn))
            progressed = after_done_count - before_done_count
            remaining = len(keywords) - after_done_count
            print(f"[INFO] round={round_idx} end progress={progressed} remaining={remaining}")
            if remaining > 0 and progressed <= 0:
                await asyncio.sleep(1.0)
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
