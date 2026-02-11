const EXT_BUILD = "2026-02-10-ddddocr-v2";

const BG_SELECTORS = [
  ".yidun_bg-img",
  ".yidun_bgimg .yidun_bg-img",
  "img.yidun_bg-img",
];
const PIECE_SELECTORS = [
  ".yidun_jigsaw",
  "img.yidun_jigsaw",
  ".yidun_jigsaw-img",
];
const SLIDER_SELECTORS = [
  ".yidun_control .yidun_slider",
  ".yidun_slider",
  ".yidun_control [class*='yidun_slider']",
];
const CAPTCHA_CONTAINER_SELECTORS = [
  ".yidun_modal",
  ".yidun_panel",
  ".yidun_control",
  ".yidun",
];

const LOW_CONF_LEVELS = new Set(["low"]);
const AUTO_RETRY_MAX = 2;
const LOW_CONF_RETRY_MAX = 1;

let solveInProgress = false;
let autoRetryTimer = null;

console.log("[captcha-ext] content loaded", EXT_BUILD);

function queryFirst(selectors) {
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el) return el;
  }
  return null;
}

function isElementVisible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  const style = window.getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) > 0;
}

function waitForElement(selectors, validate, timeoutMs = 15000, intervalMs = 200) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const timer = setInterval(() => {
      const el = queryFirst(selectors);
      if (el && (!validate || validate(el))) {
        clearInterval(timer);
        resolve(el);
        return;
      }
      if (Date.now() - start > timeoutMs) {
        clearInterval(timer);
        reject(new Error(`wait timeout: ${selectors.join(" | ")}`));
      }
    }, intervalMs);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractUrlFromStyle(styleValue) {
  if (!styleValue) return "";
  const m = String(styleValue).match(/url\(["']?(.*?)["']?\)/i);
  return m ? String(m[1] || "").trim() : "";
}

function getImageUrl(el) {
  if (!el) return "";
  if (typeof el.currentSrc === "string" && el.currentSrc.trim()) return el.currentSrc.trim();
  if (typeof el.src === "string" && el.src.trim()) return el.src.trim();
  const inline = extractUrlFromStyle(el.style?.backgroundImage || "");
  if (inline) return inline;
  const computed = extractUrlFromStyle(window.getComputedStyle(el).backgroundImage || "");
  return computed;
}

async function waitForImageStable(imageEl, rounds = 3, intervalMs = 120) {
  if (!imageEl) throw new Error("image element missing");
  let prev = "";
  let stable = 0;
  const begin = Date.now();

  while (Date.now() - begin < 3000) {
    const url = getImageUrl(imageEl);
    const w = Number(imageEl.naturalWidth || imageEl.width || 0);
    const h = Number(imageEl.naturalHeight || imageEl.height || 0);
    const key = `${url}|${w}|${h}`;
    if (url && w > 0 && h > 0 && key === prev) {
      stable += 1;
      if (stable >= rounds) return { url, w, h };
    } else {
      stable = 0;
      prev = key;
    }
    await sleep(intervalMs);
  }

  const fallbackUrl = getImageUrl(imageEl);
  const fw = Number(imageEl.naturalWidth || imageEl.width || 0);
  const fh = Number(imageEl.naturalHeight || imageEl.height || 0);
  if (fallbackUrl && fw > 0 && fh > 0) {
    return { url: fallbackUrl, w: fw, h: fh };
  }
  throw new Error("image not stable");
}

function getSliderHandle(slider) {
  if (!slider) return null;
  const control = slider.closest(".yidun_control");
  if (!control) return slider;
  return control.querySelector(".yidun_slider") || slider;
}

function checkCaptchaSuccess() {
  const container = queryFirst(CAPTCHA_CONTAINER_SELECTORS);
  if (!container) return true;
  if (!isElementVisible(container)) return true;
  const bg = queryFirst(BG_SELECTORS);
  return !isElementVisible(bg);
}

function generateHumanTrack(distance) {
  const track = [];
  let current = 0;
  let v = Math.random() * 1.2 + 1.0;

  while (current < distance) {
    const remain = distance - current;
    const a = current < distance * 0.6 ? (Math.random() * 1.4 + 0.6) : -(Math.random() * 1.4 + 0.4);
    v = Math.max(0.8, Math.min(8.0, v + a));
    let step = Math.max(1, Math.round(v + (Math.random() - 0.5) * 1.5));
    if (step > remain) step = remain;
    current += step;
    track.push({ x: step, y: Math.round((Math.random() - 0.5) * 2) });
  }

  if (track.length > 5) {
    const i = Math.floor(track.length * (0.35 + Math.random() * 0.35));
    track.splice(i, 0, { x: 0, y: 0, pause: 40 + Math.floor(Math.random() * 90) });
  }
  return track;
}

function simulatePointerEvent(element, type, clientX, clientY, buttons = 0) {
  if (typeof PointerEvent !== "function") return;
  element.dispatchEvent(new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX,
    clientY,
    button: 0,
    buttons,
    pointerId: 1,
    pointerType: "mouse",
    isPrimary: true,
  }));
}

function simulateMouseEvent(element, type, clientX, clientY) {
  element.dispatchEvent(new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX,
    clientY,
    buttons: type === "mousedown" ? 1 : 0,
  }));
}

function dispatchMoveToTargets(type, x, y, buttons) {
  const hit = document.elementFromPoint(x, y);
  const targets = [hit, document].filter(Boolean);
  for (const target of targets) {
    if (type === "move") {
      simulatePointerEvent(target, "pointermove", x, y, buttons);
      simulateMouseEvent(target, "mousemove", x, y);
    } else if (type === "up") {
      simulatePointerEvent(target, "pointerup", x, y, 0);
      simulateMouseEvent(target, "mouseup", x, y);
    }
  }
}

function requestSolveFromBackend(payload) {
  return new Promise((resolve, reject) => {
    if (!chrome?.runtime?.sendMessage) {
      reject(new Error("chrome.runtime.sendMessage unavailable"));
      return;
    }
    chrome.runtime.sendMessage({ type: "SOLVE_SLIDER_CAPTCHA", payload }, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message || "runtime error"));
        return;
      }
      if (!resp?.ok) {
        reject(new Error(String(resp?.error || "backend solve failed")));
        return;
      }
      resolve(resp.data || {});
    });
  });
}

function calcDistanceFromBackend(imageX, bgNaturalWidth, trackRect, sliderRect, distanceOffsetPx = 0) {
  const scale = bgNaturalWidth > 0 ? (trackRect.width / bgNaturalWidth) : 1;
  const sliderInitOffset = sliderRect.left - trackRect.left;
  const raw = imageX * scale - sliderInitOffset - (sliderRect.width / 2) + distanceOffsetPx;
  const maxDistance = Math.max(1, trackRect.width - sliderRect.width);
  return Math.max(1, Math.min(Math.round(raw), Math.round(maxDistance)));
}

async function performDrag(sliderHandle, totalDistance) {
  const sliderRect = sliderHandle.getBoundingClientRect();
  const startX = sliderRect.left + sliderRect.width / 2;
  const startY = sliderRect.top + sliderRect.height / 2;

  simulatePointerEvent(sliderHandle, "pointermove", startX, startY);
  simulateMouseEvent(sliderHandle, "mousemove", startX, startY);
  await sleep(120 + Math.random() * 180);

  simulatePointerEvent(sliderHandle, "pointerdown", startX, startY, 1);
  simulateMouseEvent(sliderHandle, "mousedown", startX, startY);
  await sleep(80 + Math.random() * 140);

  const track = generateHumanTrack(totalDistance);
  let currentX = startX;
  let currentY = startY;

  for (const step of track) {
    if (step.pause) {
      await sleep(step.pause);
      continue;
    }
    currentX += step.x;
    currentY += step.y;
    dispatchMoveToTargets("move", currentX, currentY, 1);
    await sleep(7 + Math.random() * 24);
  }

  // 末端微调，降低边界误差导致的失败概率。
  const tweaks = [
    1 + Math.floor(Math.random() * 2),
    -(1 + Math.floor(Math.random() * 2)),
    1,
  ];
  for (const delta of tweaks) {
    currentX += delta;
    dispatchMoveToTargets("move", currentX, currentY, 1);
    await sleep(18 + Math.random() * 26);
  }

  await sleep(60 + Math.random() * 110);
  dispatchMoveToTargets("up", currentX, currentY, 0);
}

async function solveOnce(ctx) {
  const [bgImg, pieceImg, slider] = await Promise.all([
    waitForElement(BG_SELECTORS, (el) => isElementVisible(el), 12000, 180),
    waitForElement(PIECE_SELECTORS, (el) => isElementVisible(el), 12000, 180),
    waitForElement(SLIDER_SELECTORS, (el) => isElementVisible(el), 12000, 180),
  ]);

  const [{ url: bgUrl, w: bgW }, { url: pieceUrl }] = await Promise.all([
    waitForImageStable(bgImg, 2, 120),
    waitForImageStable(pieceImg, 2, 120),
  ]);

  if (!bgUrl || !pieceUrl) {
    throw new Error(`image url missing bg=${Boolean(bgUrl)} piece=${Boolean(pieceUrl)}`);
  }

  const sliderHandle = getSliderHandle(slider);
  if (!sliderHandle) throw new Error("slider handle not found");

  const sliderRect = sliderHandle.getBoundingClientRect();
  const trackEl = sliderHandle.closest(".yidun_slider_track") || sliderHandle.closest(".yidun_control") || sliderHandle;
  const trackRect = trackEl.getBoundingClientRect();

  const backend = await requestSolveFromBackend({
    bg_url: bgUrl,
    piece_url: pieceUrl,
    page_url: location.href,
    ua: navigator.userAgent,
    ts: Date.now(),
  });

  const imageX = Number(backend.image_x ?? backend.raw_x ?? backend.x ?? -1);
  if (!Number.isFinite(imageX) || imageX < 0) {
    throw new Error(`invalid backend image_x=${backend.image_x}`);
  }

  const confidenceLevel = String(backend.confidence_level || "").toLowerCase();
  const confidence = Number(backend.confidence ?? 0);
  const backendDistanceOffset = Number(backend.distance_offset_px ?? 0);
  const distanceOffsetPx = Number.isFinite(backendDistanceOffset) ? backendDistanceOffset : 0;

  if (LOW_CONF_LEVELS.has(confidenceLevel) && ctx.lowConfRetries < LOW_CONF_RETRY_MAX) {
    console.log("[captcha-ext] low confidence, retry once", {
      confidenceLevel,
      confidence,
      strategy: backend.strategy,
    });
    await sleep(240 + Math.random() * 260);
    return solveOnce({ ...ctx, lowConfRetries: ctx.lowConfRetries + 1 });
  }

  const bgNaturalWidth = Number(bgImg.naturalWidth || bgImg.width || bgW || backend.bg_width || 0);
  const totalDistance = calcDistanceFromBackend(imageX, bgNaturalWidth, trackRect, sliderRect, distanceOffsetPx);

  await performDrag(sliderHandle, totalDistance);

  await sleep(1600);
  if (!checkCaptchaSuccess()) {
    throw new Error("captcha still visible after drag");
  }

  console.log("[captcha-ext] solved", {
    imageX,
    totalDistance,
    distanceOffsetPx,
    bgNaturalWidth,
    confidence,
    confidenceLevel,
    strategy: backend.strategy,
  });
  return { ok: true };
}

async function solveSliderCaptcha(options = {}) {
  const opts = {
    source: "auto",
    retryCount: 0,
    autoRetry: true,
    ...options,
  };

  if (solveInProgress) {
    return { ok: false, error: "solve busy" };
  }
  solveInProgress = true;

  try {
    const result = await solveOnce({ lowConfRetries: 0 });
    return result;
  } catch (err) {
    console.log("[captcha-ext] solve failed", err);

    if (opts.autoRetry && opts.retryCount < AUTO_RETRY_MAX) {
      if (autoRetryTimer) clearTimeout(autoRetryTimer);
      const nextRetry = opts.retryCount + 1;
      const waitMs = 1200 + nextRetry * 500;
      autoRetryTimer = setTimeout(() => {
        solveSliderCaptcha({ source: "auto-retry", retryCount: nextRetry, autoRetry: true }).catch(() => {});
      }, waitMs);
    }

    return { ok: false, error: String(err?.message || err) };
  } finally {
    solveInProgress = false;
  }
}

function installTestButton() {
  if (!document.body) return;
  if (document.getElementById("codex-captcha-test-btn")) return;

  const wrap = document.createElement("div");
  wrap.id = "codex-captcha-test-wrap";
  wrap.style.cssText = [
    "position: fixed",
    "right: 14px",
    "bottom: 14px",
    "z-index: 2147483647",
    "display: flex",
    "flex-direction: column",
    "gap: 6px",
    "align-items: flex-end",
  ].join(";");

  const btn = document.createElement("button");
  btn.id = "codex-captcha-test-btn";
  btn.textContent = "验证码测试";
  btn.style.cssText = [
    "padding: 10px 14px",
    "border: 0",
    "border-radius: 8px",
    "background: #1677ff",
    "color: #fff",
    "font-size: 13px",
    "cursor: pointer",
    "box-shadow: 0 8px 18px rgba(0,0,0,.2)",
  ].join(";");

  const status = document.createElement("div");
  status.id = "codex-captcha-test-status";
  status.textContent = "待命";
  status.style.cssText = [
    "padding: 4px 8px",
    "border-radius: 6px",
    "background: rgba(0,0,0,.65)",
    "color: #fff",
    "font-size: 12px",
    "line-height: 1.2",
  ].join(";");

  btn.addEventListener("click", async () => {
    if (btn.dataset.running === "1") return;
    btn.dataset.running = "1";
    btn.textContent = "测试中...";
    status.textContent = "正在发送当前页面验证码到后端";
    const r = await solveSliderCaptcha({ source: "manual-test", retryCount: 0, autoRetry: false });
    status.textContent = r.ok ? "完成" : `失败: ${String(r.error || "unknown")}`;
    btn.dataset.running = "0";
    btn.textContent = "验证码测试";
  });

  wrap.appendChild(btn);
  wrap.appendChild(status);
  document.body.appendChild(wrap);
}

function maybeAutoStart() {
  const hasCaptcha = Boolean(queryFirst(BG_SELECTORS) && queryFirst(PIECE_SELECTORS) && queryFirst(SLIDER_SELECTORS));
  if (hasCaptcha) {
    solveSliderCaptcha({ source: "load", retryCount: 0, autoRetry: true }).catch(() => {});
  }
}

window.addEventListener("load", () => {
  setTimeout(maybeAutoStart, 500);
  setTimeout(installTestButton, 200);
});

window.addEventListener("codex-run-captcha", () => {
  solveSliderCaptcha({ source: "event", retryCount: 0, autoRetry: true }).catch(() => {});
});

const observer = new MutationObserver(() => {
  if (solveInProgress) return;
  const hasCaptcha = Boolean(queryFirst(BG_SELECTORS) && queryFirst(PIECE_SELECTORS) && queryFirst(SLIDER_SELECTORS));
  if (hasCaptcha) {
    setTimeout(() => {
      if (!solveInProgress) {
        solveSliderCaptcha({ source: "mutation", retryCount: 0, autoRetry: true }).catch(() => {});
      }
    }, 250);
  }
});

observer.observe(document.documentElement || document.body, {
  childList: true,
  subtree: true,
});
