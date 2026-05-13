"""
监控与盈亏统计：内存聚合 + 日志告警钩子
====================================
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, DefaultDict, Dict, List, Optional

from loguru import logger

if TYPE_CHECKING:
    from bot.telegram_bot import TelegramBot


@dataclass
class TradeRecord:
    symbol: str
    ts: float
    side: str
    qty: float
    price: float
    fee: float
    pnl: float
    tag: str = ""


@dataclass
class PnLMonitor:
    """简单实时统计（实盘/回测均可复用）。"""

    session_start: float = field(default_factory=time.time)
    realized_pnl: float = 0.0
    trades: List[TradeRecord] = field(default_factory=list)
    symbol_pnl: DefaultDict[str, float] = field(default_factory=lambda: defaultdict(float))

    def record_trade(self, rec: TradeRecord) -> None:
        self.trades.append(rec)
        self.realized_pnl += rec.pnl - rec.fee
        self.symbol_pnl[rec.symbol] += rec.pnl - rec.fee
        logger.info(
            "成交统计 | {} {} qty={} price={} pnl={:.4f} fee={:.4f} cum={:.4f}",
            rec.symbol, rec.side, rec.qty, rec.price,
            rec.pnl, rec.fee, self.realized_pnl,
        )

    def summary(self) -> Dict[str, float]:
        dur_min = (time.time() - self.session_start) / 60.0
        return {
            "duration_min": dur_min,
            "realized_pnl": self.realized_pnl,
            "trade_count": float(len(self.trades)),
        }


# ─── TG 实例（由 live_loop 注入，monitoring 模块直接使用）────────

_tg: Optional["TelegramBot"] = None


def set_telegram(tg: "TelegramBot") -> None:
    """注入 TelegramBot 实例，供 alert_exception 等函数使用。"""
    global _tg
    _tg = tg


def alert_exception(exc: BaseException, context: str) -> None:
    """异常告警：日志 + TG 推送（若已配置）。"""
    logger.exception("{} | 未处理异常: {}", context, exc)
    if _tg is not None and _tg.enabled:
        import asyncio
        import traceback as _tb
        detail = f"<code>{context}</code>\n{_esc_html(str(exc))}"
        tb_lines = _tb.format_exc(limit=5)
        if len(tb_lines) > 300:
            tb_lines = "..." + tb_lines[-300:]
        detail += f"\n<pre>{_esc_html(tb_lines)}</pre>"
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_tg.send_alert(f"策略异常: {context}", detail, level="error"))
        except Exception:  # noqa: BLE001
            pass


def _esc_html(text: str) -> str:
    """对 HTML 特殊字符转义，防止 Telegram 解析报错。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
