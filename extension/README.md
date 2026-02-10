# 验证码扩展（ddddocr 后端联动）

## 流程
1. Chrome 刷新后，`content.js` 检测 `yidun_bg-img` 和 `yidun_jigsaw`。
2. 提取两张图的 URL，发给 `background.js`。
3. `background.js` 转发到本地后端 `POST http://127.0.0.1:5050/captcha/solve`。
4. 后端用 `ddddocr.slide_match` 返回缺口 `image_x`。
5. `content.js` 根据页面缩放换算拖动距离并自动滑动。

## 安装
1. 打开 `chrome://extensions/`。
2. 开启开发者模式。
3. 加载已解压扩展：选择 `微博h5/extension`。

## 后端启动
在 `微博h5` 目录执行：

```bash
python3 -m pip install -r requirements.txt
python3 captcha_server.py --host 127.0.0.1 --port 5050
```

健康检查：

```bash
curl http://127.0.0.1:5050/health
```

## 可调参数
- `captcha_server.py --x-offset N`：对识别出的 `image_x` 做像素补偿。
- 如需改后端地址，可在 `content.js` 消息里传 `api_base`，或改 `background.js` 的 `API_BASE`。

## 手动触发
在页面 Console：

```javascript
window.dispatchEvent(new Event('codex-run-captcha'));
```

python weibo_bulk_api.py --config ./run_config.json