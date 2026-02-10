#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
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


@dataclass
class MatchCandidate:
    strategy: str
    x: int
    score: float
    raw: Dict[str, Any]


def parse_args() -> argparse.Namespace:
    def build_parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(description="易盾滑块识别服务 (ddddocr)")
        p.add_argument("--config", default="", help="JSON配置文件路径（可与 weibo_bulk_api 共用）")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=5050)
        p.add_argument("--timeout", type=float, default=15.0, help="下载验证码图片超时")
        p.add_argument("--x-offset", type=int, default=0, help="识别x坐标补偿(像素)")
        p.add_argument("--low-confidence-threshold", type=float, default=0.62, help="低置信度阈值")
        p.add_argument("--consistency-tolerance", type=int, default=5, help="多策略结果x偏差容忍")
        p.add_argument("--debug", action="store_true")
        return p

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    pre_args, _ = pre.parse_known_args()

    parser = build_parser()
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

        cfg: Dict[str, Any] = {}
        field_map = {
            "host": "host",
            "port": "port",
            "timeout": "timeout",
            "x_offset": "x_offset",
            "low_confidence_threshold": "low_confidence_threshold",
            "consistency_tolerance": "consistency_tolerance",
            "debug": "debug",
            # 允许共用 run_config 中的 captcha_* 字段
            "captcha_host": "host",
            "captcha_port": "port",
            "captcha_timeout": "timeout",
            "captcha_x_offset": "x_offset",
            "captcha_low_confidence_threshold": "low_confidence_threshold",
            "captcha_consistency_tolerance": "consistency_tolerance",
            "captcha_debug": "debug",
        }
        for k, v in raw_cfg.items():
            if k in field_map:
                cfg[field_map[k]] = v
        parser.set_defaults(**cfg)

    return parser.parse_args()


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


def parse_slide_match_result(result: Any) -> Tuple[int, float, Dict[str, Any]]:
    if not isinstance(result, dict):
        raise ValueError(f"unexpected slide_match result: {result!r}")

    x = None
    if isinstance(result.get("target"), (list, tuple)) and result["target"]:
        x = int(result["target"][0])
    if x is None:
        for key in ("x", "left", "offset", "target_x"):
            if key in result:
                x = int(float(result[key]))
                break
    if x is None:
        raise ValueError(f"x not found in slide_match result: {result}")

    score = None
    for key in ("confidence", "score", "sim", "similarity"):
        if key in result:
            try:
                score = float(result[key])
                break
            except Exception:  # noqa: BLE001
                pass
    if score is None:
        score = 0.55
    score = max(0.0, min(1.0, score))
    return x, score, result


def _decode_to_bgr(image_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise ValueError("decode image failed")
    return img


def _encode_png(image_bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise ValueError("encode image failed")
    return bytes(buf)


def _to_eq_gray_bgr(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    eq = cv2.equalizeHist(gray)
    return cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)


def _to_edge_bgr(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edge = cv2.Canny(blur, 50, 150)
    return cv2.cvtColor(edge, cv2.COLOR_GRAY2BGR)


def _to_denoise_eq_bgr(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    denoise = cv2.bilateralFilter(gray, d=5, sigmaColor=40, sigmaSpace=40)
    eq = cv2.equalizeHist(denoise)
    return cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)


def build_variants(bg_bytes: bytes, piece_bytes: bytes) -> Dict[str, Tuple[bytes, bytes]]:
    bg = _decode_to_bgr(bg_bytes)
    piece = _decode_to_bgr(piece_bytes)

    variants: Dict[str, Tuple[bytes, bytes]] = {
        "raw": (bg_bytes, piece_bytes),
        "eq_gray": (_encode_png(_to_eq_gray_bgr(bg)), _encode_png(_to_eq_gray_bgr(piece))),
        "denoise_eq": (_encode_png(_to_denoise_eq_bgr(bg)), _encode_png(_to_denoise_eq_bgr(piece))),
        "edge": (_encode_png(_to_edge_bgr(bg)), _encode_png(_to_edge_bgr(piece))),
    }
    return variants


def run_candidates(solver: Any, variants: Dict[str, Tuple[bytes, bytes]]) -> List[MatchCandidate]:
    out: List[MatchCandidate] = []
    methods = [
        ("simple", True),
        ("edge", False),
    ]

    for variant_name, (bg_v, piece_v) in variants.items():
        for method_name, simple_target in methods:
            strategy = f"{variant_name}:{method_name}"
            try:
                raw = solver.slide_match(piece_v, bg_v, simple_target=simple_target)
                x, score, raw_result = parse_slide_match_result(raw)
                out.append(MatchCandidate(strategy=strategy, x=x, score=score, raw=raw_result))
            except Exception as e:  # noqa: BLE001
                APP.logger.debug("candidate failed strategy=%s err=%s", strategy, e)
    return out


def select_best(candidates: List[MatchCandidate], tolerance: int, low_thr: float) -> Dict[str, Any]:
    if not candidates:
        raise ValueError("all matching strategies failed")

    sorted_by_score = sorted(candidates, key=lambda c: c.score, reverse=True)
    best = sorted_by_score[0]

    median_x = int(statistics.median([c.x for c in candidates]))
    near_median = [c for c in candidates if abs(c.x - median_x) <= tolerance]
    if near_median:
        median_best = max(near_median, key=lambda c: c.score)
        # 如果最高分与中位簇偏差太大且中位簇有多数支持，优先中位簇
        if abs(best.x - median_x) > tolerance and len(near_median) >= max(2, len(candidates) // 2):
            best = median_best

    support = [c for c in candidates if abs(c.x - best.x) <= tolerance]
    consistency = len(support) / max(1, len(candidates))
    blended = 0.7 * best.score + 0.3 * consistency

    if blended >= 0.78 and best.score >= max(low_thr, 0.60):
        level = "high"
    elif blended >= max(0.60, low_thr):
        level = "medium"
    else:
        level = "low"

    return {
        "x": int(best.x),
        "score": round(float(best.score), 4),
        "confidence": round(float(blended), 4),
        "confidence_level": level,
        "strategy": best.strategy,
        "consistency": round(float(consistency), 4),
        "candidate_count": len(candidates),
        "median_x": median_x,
        "support_count": len(support),
        "candidates": [
            {"strategy": c.strategy, "x": c.x, "score": round(float(c.score), 4)} for c in sorted_by_score
        ],
    }


@APP.get("/health")
def health() -> Any:
    if ddddocr is None:
        return jsonify({"ok": False, "error": f"ddddocr import failed: {_IMPORT_ERROR}"}), 500
    try:
        _ = get_solver()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"solver init failed: {type(e).__name__}: {e}"}), 500
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
        variants = build_variants(bg_bytes, piece_bytes)
        candidates = run_candidates(solver, variants)

        chosen = select_best(
            candidates=candidates,
            tolerance=max(1, int(args.consistency_tolerance)),
            low_thr=float(args.low_confidence_threshold),
        )

        image_x = int(chosen["x"]) + int(args.x_offset)
        if image_x < 0:
            image_x = 0

        return jsonify(
            {
                "ok": True,
                "image_x": image_x,
                "raw_x": int(chosen["x"]),
                "x_offset": int(args.x_offset),
                "confidence": chosen["confidence"],
                "confidence_level": chosen["confidence_level"],
                "strategy": chosen["strategy"],
                "consistency": chosen["consistency"],
                "candidate_count": chosen["candidate_count"],
                "support_count": chosen["support_count"],
                "bg_bytes": len(bg_bytes),
                "piece_bytes": len(piece_bytes),
                "candidates": chosen["candidates"],
                "hint": "plugin should map image_x to track distance by page scale",
            }
        )
    except Exception as exc:  # noqa: BLE001
        APP.logger.exception("captcha solve failed")
        return jsonify({"ok": False, "error": f"solve failed: {type(exc).__name__}: {exc}"}), 500


def main() -> None:
    args = parse_args()
    APP.config["ARGS"] = args

    level = logging.DEBUG if args.debug else logging.INFO
    APP.logger.setLevel(level)

    APP.run(host=args.host, port=args.port, debug=bool(args.debug))


if __name__ == "__main__":
    main()
