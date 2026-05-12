"""
程序入口：支持 --backtest / --live / --live --paper
=====================================
新增 --strategy 参数选择策略：
  dual_ma_rsi    （默认）双均线 + RSI + 波动率自适应
  bollinger      布林带突破 + 成交量 + ATR 假突破过滤
  order_flow     订单流（盘口 Delta + 大单 + 资金费率）
  ml             机器学习（LightGBM / XGBoost / LSTM）
  ensemble       多策略融合（默认使用 bollinger + order_flow + dual_ma_rsi）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from binance.client import Client
from loguru import logger

from bot.app.live_loop import LiveTrader
from bot.backtest.engine import FuturesBacktestEngine
from bot.data.klines import KlineService
from bot.logging_setup import setup_logging
from bot.strategy.base import StrategyBase
from bot.strategy.bollinger_breakout import BollingerBreakoutParams, BollingerBreakoutStrategy
from bot.strategy.dual_ma_rsi import DualMaRsiVolatilityStrategy
from bot.strategy.ensemble import EnsembleParams, EnsembleStrategy
from bot.strategy.ml_strategy import MLStrategy, MLStrategyParams
from bot.strategy.order_flow import OrderFlowParams, OrderFlowStrategy
from config import load_config


# ─── 策略工厂 ──────────────────────────────────────────────────────

def build_strategy(name: str) -> StrategyBase:
    """根据名称实例化对应策略（参数可在此处或 .env 中覆盖）。"""
    if name == "dual_ma_rsi":
        return DualMaRsiVolatilityStrategy()

    if name == "bollinger":
        return BollingerBreakoutStrategy(BollingerBreakoutParams())

    if name == "order_flow":
        return OrderFlowStrategy(OrderFlowParams())

    if name == "ml":
        return MLStrategy(MLStrategyParams())

    if name == "ml_lgbm":
        return MLStrategy(MLStrategyParams(model_backend="lgbm"))

    if name == "ml_xgb":
        return MLStrategy(MLStrategyParams(model_backend="xgb"))

    if name == "ml_lstm":
        return MLStrategy(MLStrategyParams(model_backend="lstm"))

    if name == "ensemble":
        subs = [
            DualMaRsiVolatilityStrategy(),
            BollingerBreakoutStrategy(),
            OrderFlowStrategy(),
        ]
        return EnsembleStrategy(
            subs,
            EnsembleParams(
                mode="weighted_vote",
                score_threshold=0.50,
                strategy_weights={
                    "dual_ma_rsi": 1.0,
                    "bollinger_breakout": 1.2,
                    "order_flow": 0.8,
                },
            ),
        )

    if name == "ensemble_ml":
        subs = [
            DualMaRsiVolatilityStrategy(),
            BollingerBreakoutStrategy(),
            MLStrategy(MLStrategyParams()),
        ]
        return EnsembleStrategy(
            subs,
            EnsembleParams(
                mode="weighted_vote",
                score_threshold=0.50,
                strategy_weights={
                    "dual_ma_rsi": 0.8,
                    "bollinger_breakout": 1.0,
                    "ml_strategy": 1.5,
                },
            ),
        )

    raise ValueError(
        f"未知策略名称: {name!r}。可选: dual_ma_rsi / bollinger / order_flow / "
        "ml / ml_lgbm / ml_xgb / ml_lstm / ensemble / ensemble_ml"
    )


# ─── 命令实现 ──────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Binance USDT-M 永续量化机器人")
    p.add_argument("--backtest", action="store_true", help="运行历史回测")
    p.add_argument("--live", action="store_true", help="运行实盘循环")
    p.add_argument(
        "--paper",
        action="store_true",
        help="与 --live 连用：纸面模拟，不真实下单（适合只读 API）",
    )
    p.add_argument(
        "--strategy",
        type=str,
        default="dual_ma_rsi",
        help=(
            "策略名称: dual_ma_rsi(默认) | bollinger | order_flow | "
            "ml | ml_lgbm | ml_xgb | ml_lstm | ensemble | ensemble_ml"
        ),
    )
    p.add_argument("--symbol", type=str, default="BTCUSDT", help="回测/单标的调试交易对")
    p.add_argument("--interval", type=str, default="15m", help="回测主周期")
    p.add_argument("--limit", type=int, default=1500, help="回测 K 线数量")
    return p


async def cmd_backtest(symbol: str, interval: str, limit: int, strategy_name: str) -> None:
    cfg = load_config()
    strategy = build_strategy(strategy_name)
    logger.info("回测策略: {} | 标的: {} | 周期: {} | K线数: {}", strategy.name, symbol, interval, limit)
    client = Client(testnet=cfg.binance_testnet, ping=False)
    ks = KlineService(client)
    df = ks.fetch_klines_sync(symbol, interval, limit=limit)
    engine = FuturesBacktestEngine(cfg, strategy)
    res = engine.run(df, symbol=symbol)
    print(json.dumps(res.metrics, indent=2, ensure_ascii=False))


async def cmd_live(*, paper: bool, strategy_name: str) -> None:
    cfg = load_config()
    if paper:
        logger.info(
            "纸面模式：不会发送真实委托；请在币安控制台为 API 勾选「允许读取」及「允许合约」以便拉取账户/仓位（可选）。"
        )
    else:
        cfg.validate_live_keys()

    strategy = build_strategy(strategy_name)
    logger.info("实盘策略: {}", strategy.name)
    trader = LiveTrader(cfg, strategy, primary_interval="15m", paper_mode=paper)
    await trader.run_forever(interval_sec=30.0)


def main() -> None:
    setup_logging()
    args = build_arg_parser().parse_args()
    if args.backtest and args.live:
        logger.error("请只选择 --backtest 或 --live 之一")
        sys.exit(2)
    if args.paper and not args.live:
        logger.error("--paper 必须与 --live 同时使用，例如: python main.py --live --paper")
        sys.exit(2)
    try:
        if args.backtest:
            asyncio.run(cmd_backtest(args.symbol.upper(), args.interval, args.limit, args.strategy))
        elif args.live:
            asyncio.run(cmd_live(paper=args.paper, strategy_name=args.strategy))
        else:
            logger.error("必须指定 --backtest 或 --live")
            sys.exit(2)
    except ValueError as e:
        logger.error("{}", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
