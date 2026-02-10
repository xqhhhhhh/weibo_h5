const API_BASE = "http://127.0.0.1:5050";

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode.apply(null, chunk);
  }
  return btoa(binary);
}

async function fetchAsDataUrl(url) {
  const resp = await fetch(url, { credentials: "omit", cache: "no-store" });
  if (!resp.ok) {
    throw new Error(`fetch failed status=${resp.status}`);
  }
  const blob = await resp.blob();
  const ab = await blob.arrayBuffer();
  const bytes = new Uint8Array(ab);
  const mime = blob.type || "image/png";
  return `data:${mime};base64,${bytesToBase64(bytes)}`;
}

async function postJson(url, payload) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
    cache: "no-store",
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`http ${resp.status} ${text}`.trim());
  }
  return resp.json();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "SOLVE_SLIDER_CAPTCHA") return;

  const payload = { ...(msg.payload || {}) };
  const apiBase = String(payload.api_base || API_BASE).replace(/\/$/, "");

  (async () => {
    try {
      if (payload.bg_url && !String(payload.bg_url).startsWith("data:")) {
        payload.bg_url = await fetchAsDataUrl(String(payload.bg_url));
      }
      if (payload.piece_url && !String(payload.piece_url).startsWith("data:")) {
        payload.piece_url = await fetchAsDataUrl(String(payload.piece_url));
      }
    } catch (fetchErr) {
      // 如果浏览器侧抓图失败，回退到原始URL让后端兜底下载
      console.warn("[captcha-ext] fetchAsDataUrl failed, fallback to original URLs", fetchErr);
    }

    postJson(`${apiBase}/captcha/solve`, payload)
      .then((data) => {
        sendResponse({ ok: true, data });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err) });
      });
  })();

  return true;
});
