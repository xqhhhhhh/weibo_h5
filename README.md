# 微博关键词爬虫（API 串行版 + 验证码自动识别）

核心脚本：`weibo_bulk_api.py`

当前版本包含完整的验证码自动识别功能，支持视觉识别和JS逆向两种方式。

## 安装

```bash
cd 微博h5
python3 -m pip install -r requirements.txt
```

## 输入文件

1. 关键词 CSV（`--csv`）
- 有表头时自动识别：`keyword` / `keywords` / `关键词` / `关键字` / `query` / `topic` / `话题`
- 无表头时默认读取第一列

2. 账号 JSON（`--accounts`）
- 必须是数组
- 每个账号至少要有 `cookie`
- 可选字段：`name`、`user_agent`、`referer`、`accept_language`、`qps`

## 运行示例

```bash
cd 微博h5
python3 weibo_bulk_api.py \
  --csv ./keywors.csv \
  --accounts ./accounts.json \
  --output ./output/weibo_bulk_result.jsonl \
  --state-db ./output/weibo_bulk_state.db \
  --per-account-qps 1.0 \
  --max-retries 3 \
  --max-media-pages 12 \
  --max-contrib-pages 3 \
  --timeout 20 \
  --allow-empty-contrib \
  --refresh-on-not-found \
  --refresh-method auto \
  --refresh-wait 5 \
  --verify-poll-interval 2 \
  --verify-cycle-timeout 45 \
  --refresh-url-keyword weibo \
  --refresh-window-keyword Chrome
```

也可以把参数放进 JSON 配置文件：

```bash
python weibo_bulk_api.py --config ./run_config.json
```

示例 `run_config.json`：

```json
{
  "csv": "./keywors.csv",
  "accounts": "./accounts.json",
  "output": "./output/weibo_bulk_result.jsonl",
  "state_db": "./output/weibo_bulk_state.db",
  "per_account_qps": 1.0,
  "max_retries": 3,
  "max_media_pages": 12,
  "max_contrib_pages": 3,
  "timeout": 20,
  "allow_empty_contrib": true,
  "refresh_on_not_found": true,
  "refresh_method": "auto",
  "refresh_wait": 5,
  "verify_poll_interval": 2,
  "verify_cycle_timeout": 45,
  "refresh_url_keyword": "weibo",
  "refresh_window_keyword": "Chrome"
}
```

命令行参数优先级高于配置文件（同名参数会覆盖配置值）。

## 参数说明（与当前脚本一致）

- `--csv`：关键词 CSV 路径（必填）
- `--accounts`：账号配置 JSON 路径（必填）
- `--config`：JSON 配置文件路径（可把运行参数集中放在文件里）
- `--output`：结果 JSONL 输出路径（默认 `output/weibo_bulk_result.jsonl`）
- `--raw-log`：每次接口原始返回 JSONL 路径（默认不记录）
- `--state-db`：断点续跑 SQLite 路径（默认 `output/weibo_bulk_state.db`）
- `--keyword-column`：手动指定关键词列名
- `--per-account-qps`：单账号 QPS（默认 `2.0`）
- `--timeout`：请求超时秒数（默认 `20`）
- `--max-retries`：失败重试次数（默认 `3`）
- `--max-media-pages`：媒体列表最大翻页数（默认 `12`）
- `--max-contrib-pages`：贡献榜最大翻页数（默认 `3`）
- `--allow-empty-contrib`：贡献榜为空也算成功
- `--limit`：只跑前 N 个关键词（调试用）
- `--refresh-on-not-found`：命中 `found=false` 时暂停并刷新本地 Chrome
- `--refresh-method`：刷新方式，`auto`/`mac`/`windows`（默认 `auto`）
- `--refresh-wait`：验证成功后等待秒数（默认 `5.0`）
- `--verify-poll-interval`：验证码状态轮询间隔秒数（默认 `2.0`）
- `--verify-cycle-timeout`：单轮验证等待超时秒数，超时后自动再次刷新（默认 `45.0`）
- `--refresh-url-keyword`：`mac` 模式下要刷新的标签页 URL 关键字（默认 `weibo`）
- `--refresh-window-keyword`：`windows` 模式下激活窗口标题关键字（默认 `Chrome`）

## 输出字段

- `keyword`
- `found`
- `media_publish_count`
- `host`
- `publish_media_list`（`uid` / `screen_name`）
- `top_contributors`（`rank` / `uid` / `name` / `contribution_value`）

## 断点续跑

状态记录在 `--state-db`（SQLite），已成功关键词会自动跳过。结果会追加写入 `--output`（JSONL）。

## 验证码自动识别扩展

### 功能特点
- **双模式识别**：支持视觉识别和JS逆向两种验证码破解方式
- **智能切换**：自动模式下优先尝试JS逆向，失败后备用视觉识别
- **多种逆向技术**：Canvas Hook、网络拦截、全局变量搜索等
- **真人模拟**：高度仿真的拖动轨迹和时间间隔

### 安装和使用
1. 在Chrome浏览器中打开 `chrome://extensions/`
2. 启用"开发者模式"
3. 点击"加载已解压的扩展程序"
4. 选择项目目录下的 `extension` 文件夹

### 测试页面
提供专门的测试页面 `test_captcha.html` 用于验证扩展功能：
- 视觉识别测试
- JS逆向测试
- 自动模式测试
- 调试信息查看

### 技术原理
**视觉识别模式**：
- 分析验证码背景图的阴影特征
- 通过灰度值和像素密度定位缺口位置
- 适用于大多数传统滑块验证码

**JS逆向模式**：
- Hook Canvas 绘制方法捕获原始图像
- 拦截网络请求获取验证码数据
- 搜索全局变量中的位置信息
- 直接从JavaScript对象中提取缺口坐标

## 注意事项

- 账号 Cookie 失效时，接口成功率会显著下降。
- `accounts.json` 里可为每个账号单独设置 `qps`，未设置时使用 `--per-account-qps`。
- `mac` 模式通过 AppleScript 刷新 URL 匹配标签页；`windows` 模式通过 PowerShell 激活 Chrome 窗口并发送 `Ctrl+R`。
- `mac/auto(mac)` 下，命中 `found=false` 后会进入"验证闸门"：仅当检测到验证码消失（插件验证成功）后才恢复爬取；若未成功会持续刷新并重试。
- `mac/auto(mac)` 下推荐开启 Chrome：`查看 > 开发者 > 允许 Apple 事件中的 JavaScript`（未开启时会自动降级为 URL 状态检测）。
