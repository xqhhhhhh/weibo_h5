# 微博关键词爬虫（多账号 + 验证码自动处理）

核心脚本：
- `weibo_bulk_api.py`：批量抓关键词结果，支持断点续跑、并发与分片。
- `captcha_server.py`：本地滑块识别服务（给 Chrome 扩展调用）。
- `extension/`：Chrome 扩展，自动识别/拖动验证码。

## 安装

```bash
cd 微博h5
python3 -m pip install -r requirements.txt
```

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

字段说明：
- `name`：账号名称（日志里显示）。
- `cookie`：必填。
- `qps`：账号级限速，未填时使用 `run_config.json` 的 `per_account_qps`。
- `refresh_method`：`auto` / `mac` / `windows`。
- `refresh_url_keyword`：刷新/检测目标标签页 URL 关键词。
- `refresh_window_keyword`：Windows 下激活窗口标题关键词。
- `refresh_window_index`：mac 下按窗口序号绑定（1 开始，`0` 表示不指定）。
- `refresh_window_tag`：mac 下优先按 `window.name` 绑定标签页（推荐）。

在 Chrome 目标页 DevTools Console 设置标签：

```javascript
window.name = "acc1";
```

## run_config.json（主配置）

`weibo_bulk_api.py` 当前实际支持的关键字段：
- 输入输出：`csv`、`accounts`、`output`、`state_db`、`raw_log`、`keyword_column`
- 抓取控制：`per_account_qps`、`concurrency`、`timeout`、`max_retries`、`max_media_pages`、`max_contrib_pages`、`allow_empty_contrib`、`limit`
- 验证码闸门：`refresh_on_not_found`、`retry_false_after_verify`、`refresh_method`、`refresh_wait`、`verify_poll_interval`、`verify_cycle_timeout`、`refresh_url_keyword`、`refresh_window_keyword`、`refresh_window_index`、`refresh_window_tag`
- 账号策略：`strict_account_isolation`、`fallback_to_other_accounts`
- 验证码服务共用配置：`captcha_*`（供 `captcha_server.py --config` 读取）

## 启动顺序

### 1) 启动验证码后端

```bash
cd 微博h5
python3 captcha_server.py --config ./run_config.json
```

健康检查：

```bash
curl http://127.0.0.1:5050/health
```

### 2) 安装并启用 Chrome 扩展

- 打开 `chrome://extensions/`
- 开启开发者模式
- 加载已解压扩展：`微博h5/extension`

### 3) 启动爬虫（单进程）

```bash
python3 weibo_bulk_api.py --config ./run_config.json
```

## 分片并行（多进程）


```bash
python3 weibo_bulk_api.py --config ./run_config.json --shard-index 0 --shard-total 4 --output ./output/weibo_bulk_result.p0.jsonl --state-db ./output/weibo_bulk_state.p0.db > ./output/weibo_bulk_worker0.log 2>&1 &
python3 weibo_bulk_api.py --config ./run_config.json --shard-index 1 --shard-total 4 --output ./output/weibo_bulk_result.p1.jsonl --state-db ./output/weibo_bulk_state.p1.db > ./output/weibo_bulk_worker1.log 2>&1 &
python3 weibo_bulk_api.py --config ./run_config.json --shard-index 2 --shard-total 4 --output ./output/weibo_bulk_result.p2.jsonl --state-db ./output/weibo_bulk_state.p2.db > ./output/weibo_bulk_worker2.log 2>&1 &
python3 weibo_bulk_api.py --config ./run_config.json --shard-index 3 --shard-total 4 --output ./output/weibo_bulk_result.p3.jsonl --state-db ./output/weibo_bulk_state.p3.db > ./output/weibo_bulk_worker3.log 2>&1 &
```

查看进程：

```bash
ps aux | rg "weibo_bulk_api.py.*--shard-index" | rg -v rg
```

停止分片：

```bash
pkill -f "weibo_bulk_api.py --config ./run_config.json --shard-index"
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

