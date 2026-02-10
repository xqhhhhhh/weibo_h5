#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
from typing import Any, Dict, Tuple

import requests
from flask import Flask, jsonify, request

try:
    import ddddocr
except Exception as exc:  # noqa: BLE001
    ddddocr = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

APP = Flask(__name__)
_HTTP = requests.Session()
_SOLVER = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="易盾滑块识别服务 (ddddocr)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--timeout", type=float, default=15.0, help="下载验证码图片超时")
    p.add_argument("--x-offset", type=int, default=0, help="识别x坐标补偿(像素)")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def _as_bytes_from_data_url(data_url: str) -> bytes:
    m = re.match(r"^data:.*?;base64,(.*)$", data_url, flags=re.IGNORECASE)
    if not m:
        raise ValueError("invalid data url")
    return base64.b64decode(m.group(1))


def fetch_image_bytes(url: str, timeout: float, referer: str = "") -> bytes:
    url = str(url or "").strip()
    if not url:
        raise ValueError("empty image url")

    if url.startswith("data:"):
        return _as_bytes_from_data_url(url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        )
    }
    if referer:
        headers["Referer"] = referer

    resp = _HTTP.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    if not resp.content:
        raise ValueError(f"empty response body for {url}")
    return resp.content


def get_solver() -> Any:
    global _SOLVER
    if _SOLVER is not None:
        return _SOLVER
    if ddddocr is None:
        raise RuntimeError(f"ddddocr import failed: {_IMPORT_ERROR}")

    try:
        _SOLVER = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
    except TypeError:
        _SOLVER = ddddocr.DdddOcr(det=False, ocr=False)
    return _SOLVER


def parse_slide_match_result(result: Any) -> Tuple[int, Dict[str, Any]]:
    if not isinstance(result, dict):
        raise ValueError(f"unexpected slide_match result: {result!r}")

    if isinstance(result.get("target"), (list, tuple)) and result["target"]:
        x = int(result["target"][0])
        return x, result

    for key in ("x", "left", "offset"):
        if key in result:
            x = int(float(result[key]))
            return x, result

    raise ValueError(f"x not found in slide_match result: {result}")


@APP.get("/health")
def health() -> Any:
    if ddddocr is None:
        return jsonify({"ok": False, "error": f"ddddocr import failed: {_IMPORT_ERROR}"}), 500
    return jsonify({"ok": True})


@APP.post("/captcha/solve")
def solve_captcha() -> Any:
    args = APP.config["ARGS"]
    data = request.get_json(silent=True) or {}

    bg_url = str(data.get("bg_url") or "").strip()
    piece_url = str(data.get("piece_url") or "").strip()
    referer = str(data.get("page_url") or "").strip()

    if not bg_url or not piece_url:
        return jsonify({"ok": False, "error": "bg_url and piece_url are required"}), 400

    try:
        bg_bytes = fetch_image_bytes(bg_url, timeout=float(args.timeout), referer=referer)
        piece_bytes = fetch_image_bytes(piece_url, timeout=float(args.timeout), referer=referer)
        if not bg_bytes or not piece_bytes:
            raise ValueError("empty image bytes")

        solver = get_solver()
        raw = solver.slide_match(piece_bytes, bg_bytes, simple_target=True)
        image_x, raw_result = parse_slide_match_result(raw)

        image_x += int(args.x_offset)
        if image_x < 0:
            image_x = 0

        return jsonify(
            {
                "ok": True,
                "image_x": image_x,
                "raw": raw_result,
                "bg_bytes": len(bg_bytes),
                "piece_bytes": len(piece_bytes),
                "hint": "plugin should map image_x to track distance by page scale",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"solve failed: {type(exc).__name__}: {exc}"}), 500


def main() -> None:
    args = parse_args()
    APP.config["ARGS"] = args
    APP.run(host=args.host, port=args.port, debug=bool(args.debug))


if __name__ == "__main__":
    main()
