# Ethany-Q · Binance USDT-M 永续合约机器人

基于 **asyncio + WebSockets + python-binance** 的模块化合约机器人：**配置 → 数据（REST / WS）→ 策略 → 风控 → 执行 → 监控 / 回测 / Telegram**。

建议使用 **Python 3.11+**（3.9 亦可运行，但建议用官方 Python 或 Homebrew Python 以获得更好的 SSL 与依赖兼容性）。

---

## 功能概览

| 模块 | 说明 |
|------|------|
| **数据** | 多周期 K 线 REST；深度 + K 线组合流 WebSocket；支持分页拉取超长历史 K 线 |
| **策略** | 双均线+RSI、布林带突破、订单流、LightGBM/XGBoost/LSTM、多策略融合（Ensemble） |
| **风控** | 单笔风险、单标的名义敞口上限、组合保证金预检、最大持仓数、全局回撤熔断、标的权重 |
| **执行** | 市价单、止损/止盈市价保护单、精度对齐、限频重试；市价单成交后轮询兼容 `-2013` |
| **监控** | loguru 日志、异常告警；可选 Telegram 推送与指令查询 |
| **回测** | 手续费与滑点、绩效指标输出 |

---

## 项目目录结构

```text
binance-bot/
├── main.py                      # 入口：--backtest / --live / --paper / --strategy
├── config.py                    # pydantic-settings + .env
├── requirements.txt
├── .env.example
├── README.md
├── scripts/
│   ├── export_klines.py         # 导出 K 线 CSV（支持 --total 分页拉取）
│   └── train_ml.py              # 离线训练 ML 模型 → models/
└── bot/
    ├── types_common.py          # SignalSide、StrategySignal、OrderIntent
    ├── logging_setup.py
    ├── monitoring.py            # 告警（可接 Telegram）
    ├── telegram_bot.py          # TG 推送与 /pos、/equity 等指令
    ├── app/
    │   └── live_loop.py         # 实盘主循环（纸面 / 实盘、保证金预检）
    ├── data/
    │   ├── klines.py            # K 线 REST + fetch_klines_history 分页历史
    │   ├── market_ws.py         # 深度 + K 线 WS
    │   └── user_stream.py       # 用户数据流（占位扩展）
    ├── execution/
    │   └── futures_executor.py  # 下单、杠杆、仓位、保护单、订单轮询
    ├── strategy/
    │   ├── base.py              # StrategyBase + generate_signal(symbol, ohlcv, **kwargs)
    │   ├── indicators.py        # 技术指标（pandas / numpy）
    │   ├── features.py          # ML 特征工程（与训练脚本一致）
    │   ├── dual_ma_rsi.py
    │   ├── bollinger_breakout.py
    │   ├── order_flow.py
    │   ├── ml_strategy.py
    │   └── ensemble.py
    ├── risk/
    │   └── manager.py           # 仓位、止损止盈、追踪、回撤、position_qty
    ├── backtest/
    │   └── engine.py
    └── utils/
        └── rate_limit.py
```

---

## 安装

```bash
cd binance-bot
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：填入密钥、交易对、风控参数等
```

### macOS：LightGBM 与 OpenMP

若导入 `lightgbm` 报错 `Library not loaded: libomp.dylib`，请安装 OpenMP：

```bash
brew install libomp
```

### macOS：`NotOpenSSLWarning`（LibreSSL）

若出现 `urllib3 v2 only supports OpenSSL 1.1.1+`，本仓库已在 `requirements.txt` 中约束 `urllib3<2`。请在虚拟环境内重装依赖，或改用 **Homebrew Python 3.11+** 重建 venv。

---

## 配置说明（`.env`）

| 变量 | 含义 |
|------|------|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | API 密钥 |
| `BINANCE_TESTNET` | `true` = 合约测试网，`false` = 主网 |
| `TRADING_SYMBOLS` | 逗号分隔或 JSON 数组，如 `BTCUSDT,ETHUSDT` |
| `DEFAULT_LEVERAGE` | 默认杠杆（下单前保证金预检会用到） |
| `MARGIN_MODE` | `CROSSED` / `ISOLATED` |
| `MAX_PORTFOLIO_EXPOSURE_PCT` | 组合层面：已用 + 拟开仓保证金占权益上限（如 `0.8`） |
| `MAX_SYMBOL_EXPOSURE_PCT` | **单标的名义价值**上限 = 权益 × 该比例（与杠杆无关；再与 ATR 风险反推取较小者） |
| `MAX_RISK_PER_TRADE_PCT` | 单笔最大风险占权益（与信号 strength 相乘） |
| `MAX_OPEN_POSITIONS` | 最大同时持仓标的数 |
| `MAX_DRAWDOWN_PCT` | 全局回撤熔断阈值 |
| `SYMBOL_WEIGHTS` | 可选 JSON，如 `{"BTCUSDT":1.625,"ETHUSDT":1.625,...}`；最终数量 = 风控算出数量 × 权重（未列出标的默认 `1.0`） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` / `TG_NOTIFY_LEVEL` | Telegram 机器人与推送级别：`all` / `order` / `alert` / `off` |

完整模板见 `.env.example`。

---

## 运行方式

### 回测（可不填密钥，使用公开 K 线）

```bash
python main.py --backtest --symbol BTCUSDT --interval 15m --limit 1500 --strategy dual_ma_rsi
```

### 实盘 / 纸面

```bash
# 实盘（需密钥且开启合约交易权限）
python main.py --live --strategy ensemble

# 纸面：真实拉行情与算仓，不写单（适合只读 Key）
python main.py --live --paper --strategy dual_ma_rsi
```

### 策略选择（`--strategy`）

| 名称 | 说明 |
|------|------|
| `dual_ma_rsi` | 默认：双均线 + RSI + 波动率自适应 |
| `bollinger` | 布林带突破 + 成交量 + ATR + 假突破过滤 |
| `order_flow` | 盘口失衡、大单、资金费率等（回测无深度时会退化） |
| `ml` / `ml_lgbm` / `ml_xgb` / `ml_lstm` | 机器学习策略（需先训练并保存到 `models/`） |
| `ensemble` | 融合：双均线 + 布林带 + 订单流（加权投票） |
| `ensemble_ml` | 融合：双均线 + 布林带 + ML |

未知名称会在启动时报错并列出可选项。

---

## 机器学习：数据与训练

币安单次 K 线最多 **1500** 根；本仓库支持**分页**拉更长历史。

### 1）导出 CSV（推荐先落盘，再反复调参训练）

```bash
# 单标的：约 1 年 1h K 线（8760 根，自动分页）
python scripts/export_klines.py --symbol BTCUSDT --interval 1h --total 8760 --out data/btc_1h.csv

# 按 .env 中全部交易对批量导出到 data/
python scripts/export_klines.py --all --interval 1h --total 8760 --out-dir data/
```

### 2）离线训练

```bash
# 直接分页拉主网数据训练（默认 1h、1500 根；可用 --total 加大样本）
python scripts/train_ml.py --backend lgbm --total 8760

# 从已导出的 CSV 训练（更快、省请求）
python scripts/train_ml.py --backend lgbm --data-dir data/ --interval 1h
```

训练产物写入 `models/<SYMBOL>_lgbm.pkl`（后端不同后缀不同）。实盘使用：

```bash
python main.py --live --paper --strategy ml
```

**说明：** 测试网 K 线质量与主网差异大，**建议用主网历史数据训练**（只读即可），再在测试网或小资金实盘验证执行逻辑。

---

## Telegram 机器人

在 `.env` 配置 `TG_BOT_TOKEN`、`TG_CHAT_ID`（可先对 Bot 发 `/myid` 获取真实 Chat ID）。常用指令：

| 指令 | 作用 |
|------|------|
| `/start` `/help` | 帮助 |
| `/myid` | 显示当前 Chat ID 并校验主动推送 |
| `/pos` | 当前持仓摘要 |
| `/equity` | 权益相关 |
| `/status` | 运行状态 |
| `/signals` | 最近一轮策略信号快照 |
| `/pause` `/resume` | 暂停开新仓 / 恢复（通过风控全局状态） |

推送级别由 `TG_NOTIFY_LEVEL` 控制。

---

## 只读 API 与「模拟交易」路径

1. **纸面模式 + 主网只读 Key**：`python main.py --live --paper` — 拉行情、算信号与仓位，**不下单**；若无法读账户，会用演示权益继续跑逻辑。  
2. **合约测试网**：`.env` 中 `BINANCE_TESTNET=true` 并填写测试网密钥，`python main.py --live` — **真实撮合、假资金**。  
3. **回测**：`--backtest`，无需交易权限。

---

## 限频与 REST 使用建议

实盘主循环会对**每个标的、每个周期**发起 REST 请求拉 K 线；标的多、周期短时容易触发 **-1003（限频 / IP ban）**。建议：

- 适当拉长 `run_forever` 的扫描间隔（`main.py` 中 `interval_sec`）；  
- 优先保证 **WebSocket** 推送 K 线、REST 仅作补全或低频校验（可按需在 `market_ws` 侧扩展）。

---

## 扩展新策略

1. 继承 `bot.strategy.base.StrategyBase`，实现 `generate_signal(self, symbol, ohlcv, **kwargs) -> StrategySignal`。  
2. 实盘循环会传入 `market_cache`、`funding_rate`、`multi_tf` 等 `kwargs`，旧策略可忽略。  
3. 在 `main.py` 的 `build_strategy()` 中注册名称。  
4. 参数建议用 `dataclass`（参考各策略 `*Params`），便于回测与配置化。

---

## 部署建议

- 使用 **systemd** 或 **supervisor** 托管 `python main.py --live`，配置自动重启与日志轮转。  
- 生产环境用环境变量注入密钥，镜像与仓库中不放 `.env`。  
- API：**子账户**、**仅合约**、**关闭提币**、**IP 白名单**（若支持）。

---

## 常见问题

### `Service unavailable from a restricted location` / `restricted location`

币安按 IP / 合规策略拒绝服务，不是业务代码写错。本仓库已默认 `Client(..., ping=False)`，避免启动时调现货 `ping` 被拦。若仍被拦，需自行确认所在地区对币安服务的合规要求与服务条款。

### `APIError(code=-2019): Margin is insufficient`

可用保证金不足。程序会在实盘开仓前做预检；若已有大仓位占用保证金，新单会被跳过。请检查持仓与 `MAX_SYMBOL_EXPOSURE_PCT`、杠杆是否匹配预期。

### `APIError(code=-2013): Order does not exist`

市价单常瞬间成交，查询订单时可能已被归档；执行层已将 `-2013` 视为成交终态处理，若仍遇异常请升级至当前代码并附完整日志。

---

## 免责声明

本仓库仅供学习与研究。合约交易可能导致本金全部损失；使用者须自行承担合规与投资风险。
