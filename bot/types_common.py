"""
跨模块共享的类型定义：信号、订单方向等。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SignalSide(str, Enum):
    """策略输出方向。"""

    FLAT = "FLAT"  # 平仓 / 空仓观望
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"  # 维持现状，不主动调仓


@dataclass
class StrategySignal:
    """
    策略统一输出结构。
    generate_signal() 应返回该类型，便于执行层与回测引擎复用。
    """

    symbol: str
    side: SignalSide
    strength: float = 1.0  # 0~1，可用于仓位缩放
    meta: Optional[dict] = None  # 调试/可视化：指标快照等


@dataclass
class OrderIntent:
    """风控后的下单意图（数量、价格类型等）。"""

    symbol: str
    side: str  # BUY / SELL
    quantity: float
    reduce_only: bool = False
    position_side: str = "BOTH"  # 单向持仓模式下为 BOTH
    order_type: str = "MARKET"
    price: Optional[float] = None
    stop_price: Optional[float] = None
