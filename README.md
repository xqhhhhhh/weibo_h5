# 微博关键词爬虫（多账号 + 验证码自动处理）

核心脚本：
- `weibo_bulk_api.py`：批量抓关键词结果，支持断点续跑、并发与分片。
- `captcha_server.py`：本地滑块识别服务（给 Chrome 扩展调用）。
- `extension/`：Chrome 扩展，自动识别/拖动验证码。

## accounts.json（账号配置）

每个账号至少需要 `cookie`
需要手动在每个浏览器中设置refresh_window_tag，通过在console中输入 window.name='acc1'

```json
[
  {
    "name": "acc1",
    "cookie": "SUB=...;",
    "qps": 5.0,
    "refresh_method": "mac",
    "refresh_url_keyword": "profile/xxxx",
    "refresh_window_keyword": "Chrome",
    "refresh_window_index": 0,
    "refresh_window_tag": "acc1"
  }
]
```
### 安装并启用 Chrome 扩展

- 打开 `chrome://extensions/`
- 开启开发者模式
- 加载已解压扩展：`微博h5/extension`

## ddddocr调试
后端只能识别两张图片之间的距离差，然后传给前端，但前端本身的像素点偏移需要进行调试
通过"captcha_distance_offset_px": 25 这个参数进行调试



## 启动顺序

## 安装

```bash
cd 微博h5
python3 -m pip install -r requirements.txt
```

### 1) 启动验证码后端

```bash
cd 微博h5
python3 captcha_server.py --config ./run_config.json
```

健康检查：

```bash
curl http://127.0.0.1:5050/health
```

### 2) 启动爬虫（单进程）

```bash
python3 weibo_bulk_api.py --config ./run_config.json
```








## 输出与续跑

- 结果文件：`output/weibo_bulk_result*.jsonl`
- 断点状态库：`output/weibo_bulk_state*.db`

脚本会把成功关键词写入 `task_state`，下次启动会自动跳过已完成关键词（按对应 `state_db` 续跑）。

`output` JSONL 单行示例字段：
- `keyword`
- `found`
- `media_publish_count`
- `host`
- `publish_media_list`
- `top_contributors`

## 注意事项

- `concurrency` 会被自动限制为不超过账号数。
- `strict_account_isolation=true` 时，会自动关闭跨账号回退（等价于不启用 `fallback_to_other_accounts`）。
- 多进程时必须给每个分片使用不同的 `output` 与 `state_db`，避免写冲突。
- `refresh_window_tag` 依赖 AppleScript 执行页面 JS；若 Chrome 没开该权限会触发降级逻辑（仅 URL 检测）。

