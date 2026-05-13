"""
Telegram 集成模块
=================
功能一：推送通知（Bot → 用户）
  - 开仓 / 平仓 / 纸面模拟单
  - 异常告警
  - 全局风控熔断
  - 每日周期摘要（可选）

功能二：指令机器人（用户 → Bot）
  /start  /help    帮助菜单
  /pos             当前所有持仓详情
  /equity          账户权益 + 回撤
  /status          Bot 运行状态（轮次、策略、运行时长）
  /signals         最近一轮各标的信号快照
  /pause           暂停开新仓（设置全局熔断）
  /resume          恢复交易

使用方式：
  tg = TelegramBot(cfg)
  tg.set_trader(trader)           # 绑定 LiveTrader 供指令读取状态
  await tg.start()                # 启动指令轮询（asyncio 后台 Task）
  await tg.send_order(...)        # 主动推送下单通知
  await tg.send_alert(...)        # 推送告警
  await tg.stop()                 # 关闭

依赖：pip install "python-telegram-bot[ext]>=20.0"
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

if TYPE_CHECKING:
    from bot.app.live_loop import LiveTrader
    from config import AppConfig


# ─── 延迟导入（避免未安装时崩溃）────────────────────────────────

def _import_telegram():
    try:
        from telegram import Bot, Update
        from telegram.constants import ParseMode
        from telegram.error import TelegramError
        from telegram.ext import Application, CommandHandler, ContextTypes
        return Bot, Update, ParseMode, TelegramError, Application, CommandHandler, ContextTypes
    except ImportError:
        return None, None, None, None, None, None, None


# ─── 消息模板 ──────────────────────────────────────────────────────

_EMOJI = {
    "long":    "🟢",
    "short":   "🔴",
    "flat":    "⬜",
    "alert":   "⚠️",
    "error":   "🚨",
    "info":    "ℹ️",
    "money":   "💰",
    "chart":   "📊",
    "robot":   "🤖",
    "pause":   "⏸️",
    "play":    "▶️",
    "clock":   "🕐",
}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M:%S UTC")


def _esc(text: str) -> str:
    """对 MarkdownV2 特殊字符转义。"""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ─── 主类 ─────────────────────────────────────────────────────────

class TelegramBot:
    """
    推送通知 + 指令机器人的统一入口。
    若未配置 token，所有方法静默无操作，不影响主策略运行。
    """

    def __init__(self, cfg: "AppConfig") -> None:
        self._token: str = (cfg.tg_bot_token or "").strip()
        self._chat_id: str = (cfg.tg_chat_id or "").strip()
        self._level: str = (cfg.tg_notify_level or "all").lower()
        self._trader: Optional["LiveTrader"] = None
        self._app: Any = None          # telegram.ext.Application
        self._bot: Any = None          # telegram.Bot（用于主动推送）
        self._last_signals: dict = {}  # 保存最近一轮信号快照
        self._started: bool = False
        self._start_time: float = asyncio.get_event_loop().time() if asyncio._get_running_loop() else 0.0

        Bot, *_ = _import_telegram()
        if Bot is None:
            logger.warning("python-telegram-bot 未安装，TG 功能禁用。"
                           "执行 pip install 'python-telegram-bot[ext]>=20' 启用。")
            return
        if not self._token:
            logger.info("TG_BOT_TOKEN 未配置，TG 功能禁用。")
            return
        if not self._chat_id:
            logger.warning("TG_CHAT_ID 未配置，无法推送消息。")

        # _bot 在 start() 之前作为临时占位（用于 enabled 判断），
        # start() 完成后会替换为 Application.bot（共享连接，避免 Chat not found）
        try:
            self._bot = Bot(token=self._token)
        except Exception as e:  # noqa: BLE001
            logger.warning("Telegram Bot 初始化失败: {}", e)

    @property
    def enabled(self) -> bool:
        return self._bot is not None and bool(self._chat_id)

    def set_trader(self, trader: "LiveTrader") -> None:
        """绑定 LiveTrader，让指令处理器可以访问实时状态。"""
        self._trader = trader

    def update_signals(self, signals: dict) -> None:
        """由 live_loop 在每轮扫描后更新信号快照（供 /signals 指令使用）。"""
        self._last_signals = signals

    # ─── 内部发送 ────────────────────────────────────────────────

    async def _send(self, text: str, parse_mode: str = "HTML") -> None:
        """异步发送一条消息，失败仅记录日志不抛出。"""
        if not self.enabled:
            return
        # 优先使用 Application 内部已初始化的 bot（共享 HTTP 连接，避免 Chat not found）
        bot = self._app.bot if (self._app and self._started) else self._bot
        try:
            await bot.send_message(
                chat_id=int(self._chat_id),   # 确保传入整数类型
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("TG 发送失败: {}", e)

    # ─── 推送接口（供外部调用）──────────────────────────────────

    async def send_order(
        self,
        symbol: str,
        side: str,      # BUY / SELL
        qty: float,
        price: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        strength: float = 1.0,
        paper: bool = False,
        meta: Optional[dict] = None,
    ) -> None:
        """推送开仓通知。"""
        if self._level not in ("all", "order"):
            return
        tag = "🧾 <b>[纸面]</b>" if paper else "📋 <b>[实盘]</b>"
        emoji = _EMOJI["long"] if side == "BUY" else _EMOJI["short"]
        direction = "做多 ▲" if side == "BUY" else "做空 ▼"
        sl_str = f"{sl:.4f}" if sl else "—"
        tp_str = f"{tp:.4f}" if tp else "—"
        extra = ""
        if meta:
            kv = []
            for k in ("rsi", "bb_bw", "total_score", "p_long", "p_short"):
                if k in meta and meta[k] is not None:
                    kv.append(f"{k}={meta[k]}")
            if kv:
                extra = "\n📌 " + "  ".join(kv)
        msg = (
            f"{tag} {emoji} <b>开仓信号</b>\n"
            f"{'─'*28}\n"
            f"标的：<b>{symbol}</b>  {direction}\n"
            f"价格：<code>{price:.4f}</code>  数量：<code>{qty}</code>\n"
            f"止损：<code>{sl_str}</code>  止盈：<code>{tp_str}</code>\n"
            f"信号强度：<code>{strength:.2f}</code>"
            f"{extra}\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await self._send(msg)

    async def send_close(
        self,
        symbol: str,
        side: str,      # SELL(平多) / BUY(平空)
        qty: float,
        price: float,
        reason: str = "",
        pnl: Optional[float] = None,
        paper: bool = False,
    ) -> None:
        """推送平仓通知。"""
        if self._level not in ("all", "order"):
            return
        tag = "🧾 <b>[纸面]</b>" if paper else "📋 <b>[实盘]</b>"
        pnl_str = ""
        if pnl is not None:
            emoji_pnl = "📈" if pnl >= 0 else "📉"
            pnl_str = f"\n{emoji_pnl} 盈亏：<code>{pnl:+.2f} USDT</code>"
        msg = (
            f"{tag} ⬜ <b>平仓</b>\n"
            f"{'─'*28}\n"
            f"标的：<b>{symbol}</b>  {'平多' if side == 'SELL' else '平空'}\n"
            f"价格：<code>{price:.4f}</code>  数量：<code>{qty}</code>\n"
            f"原因：{reason or '—'}"
            f"{pnl_str}\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await self._send(msg)

    async def send_alert(self, title: str, detail: str = "", level: str = "warn") -> None:
        """推送告警/异常。"""
        if self._level == "order":
            return
        emoji = _EMOJI["error"] if level == "error" else _EMOJI["alert"]
        msg = (
            f"{emoji} <b>{title}</b>\n"
            f"{'─'*28}\n"
            f"{detail}\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await self._send(msg)

    async def send_halt(self, reason: str, drawdown: float) -> None:
        """全局熔断通知。"""
        msg = (
            f"🚨 <b>全局风控熔断！交易已暂停</b>\n"
            f"{'─'*28}\n"
            f"原因：{reason}\n"
            f"当前回撤：<code>{drawdown:.2%}</code>\n"
            f"恢复方式：发送 /resume 指令\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await self._send(msg)

    async def send_startup(self, strategy_name: str, symbols: list, paper: bool) -> None:
        """Bot 启动通知。"""
        mode = "🧾 纸面模拟" if paper else "⚡ 实盘交易"
        msg = (
            f"🤖 <b>量化机器人已启动</b>\n"
            f"{'─'*28}\n"
            f"模式：{mode}\n"
            f"策略：<code>{strategy_name}</code>\n"
            f"标的：<code>{', '.join(symbols)}</code>\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}\n"
            f"发送 /help 查看可用指令"
        )
        await self._send(msg)

    # ─── 指令处理器 ──────────────────────────────────────────────

    def _update_chat_id(self, update: Any) -> None:
        """每次收到指令时，用实际来源 chat_id 更新发送目标（自动修正配置错误）。"""
        real_id = str(update.message.chat.id)
        if real_id != self._chat_id:
            logger.info("TG chat_id 已从 {} 更新为真实值 {}", self._chat_id, real_id)
            self._chat_id = real_id

    async def _cmd_help(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        text = (
            "🤖 <b>量化机器人指令菜单</b>\n"
            "─────────────────────\n"
            "/pos       📊 当前持仓详情\n"
            "/equity    💰 账户权益与回撤\n"
            "/status    ℹ️  Bot 运行状态\n"
            "/signals   📈 最近一轮信号\n"
            "/pause     ⏸️  暂停开新仓\n"
            "/resume    ▶️  恢复交易\n"
            f"/myid      🆔 查看你的 Chat ID\n"
            "/help      📖 本菜单\n"
            "─────────────────────\n"
            f"🕐 {_now_str()}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_myid(self, update: Any, context: Any) -> None:
        """返回真实 chat_id，并自动更新配置中的 chat_id。"""
        real_id = update.message.chat.id
        self._chat_id = str(real_id)
        logger.info("TG /myid 指令：真实 chat_id = {}", real_id)
        await update.message.reply_text(
            f"🆔 <b>你的真实 Chat ID</b>\n"
            f"<code>{real_id}</code>\n\n"
            f"请将 .env 中的 <code>TG_CHAT_ID={real_id}</code> 保存。\n"
            f"Bot 已自动更新为此 ID，主动推送现在可以正常发送了。",
            parse_mode="HTML",
        )
        # 立即用新 chat_id 发一条测试消息验证
        await self._send(f"✅ 测试消息：主动推送正常！Chat ID = <code>{real_id}</code>")

    async def _cmd_pos(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if self._trader is None:
            await update.message.reply_text("⚠️ Bot 尚未绑定交易器，请稍后再试。")
            return
        try:
            raw = await self._trader.executor.get_position_risk()
            positions = self._trader._positions_from_raw(raw)
        except Exception:  # noqa: BLE001
            await update.message.reply_text("⚠️ 读取仓位失败（API 权限或网络）")
            return

        if not positions:
            await update.message.reply_text("📭 当前空仓，无持仓。", parse_mode="HTML")
            return

        lines = ["📊 <b>当前持仓</b>\n" + "─" * 28]
        for p in positions:
            direction = "多 🟢▲" if p.qty > 0 else "空 🔴▼"
            pnl_sign = "+" if p.unrealized >= 0 else ""
            roe_sign = "+" if p.roe >= 0 else ""
            liq_str = f"{p.liq_price:.4f}" if p.liq_price > 0 else "N/A"
            lines.append(
                f"<b>{p.symbol}</b>  {direction}  {p.leverage}x\n"
                f"  开仓均价：<code>{p.entry:.4f}</code>\n"
                f"  标记价格：<code>{p.mark:.4f}</code>\n"
                f"  持仓数量：<code>{abs(p.qty)}</code>\n"
                f"  爆仓价格：<code>{liq_str}</code>\n"
                f"  未实现盈亏：<code>{pnl_sign}{p.unrealized:.2f} USDT ({roe_sign}{p.roe:.2%})</code>"
            )
        lines.append(f"\n🕐 {_now_str()}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_equity(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if self._trader is None:
            await update.message.reply_text("⚠️ Bot 尚未绑定交易器。")
            return
        try:
            equity = await self._trader.executor.get_account_equity_usdt()
        except Exception:  # noqa: BLE001
            equity = None

        gs = self._trader.risk.global_state
        peak = gs.peak_equity
        dd = max(0.0, 1.0 - (equity or 0) / max(peak, 1e-8))
        halted = "⏸️ 已熔断" if gs.halted else "▶️ 运行中"

        equity_str = f"{equity:.2f}" if equity else "读取失败"
        msg = (
            f"💰 <b>账户权益</b>\n"
            f"{'─'*28}\n"
            f"当前权益：<code>{equity_str} USDT</code>\n"
            f"历史峰值：<code>{peak:.2f} USDT</code>\n"
            f"当前回撤：<code>{dd:.2%}</code>\n"
            f"交易状态：{halted}\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_status(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if self._trader is None:
            await update.message.reply_text("⚠️ Bot 尚未绑定交易器。")
            return
        import time as _time
        elapsed = _time.time() - (self._trader._start_ts if hasattr(self._trader, "_start_ts") else 0)
        h, m = divmod(int(elapsed), 3600)
        m, s = divmod(m, 60)
        run_str = f"{h}h {m}m {s}s"
        strategy_name = getattr(self._trader.strategy, "name", "unknown")
        mode_tag = "🧾 纸面" if self._trader._paper else "⚡ 实盘"
        symbols = ", ".join(self._trader.cfg.symbols)
        gs = self._trader.risk.global_state
        msg = (
            f"ℹ️ <b>Bot 状态</b>\n"
            f"{'─'*28}\n"
            f"模式：{mode_tag}\n"
            f"策略：<code>{strategy_name}</code>\n"
            f"运行时长：<code>{run_str}</code>\n"
            f"扫描轮次：<code>{self._trader._cycle_count}</code>\n"
            f"监控标的：<code>{symbols}</code>\n"
            f"交易状态：{'⏸️ 熔断' if gs.halted else '▶️ 正常'}\n"
            f"{'─'*28}\n"
            f"🕐 {_now_str()}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_signals(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if not self._last_signals:
            await update.message.reply_text("📭 暂无信号快照，等待下一轮扫描完成。")
            return
        lines = ["📈 <b>最近一轮信号</b>\n" + "─" * 28]
        for sym, sig in self._last_signals.items():
            side_emoji = {"LONG": "🟢▲", "SHORT": "🔴▼", "HOLD": "⬜—", "FLAT": "⬜✕"}.get(
                sig.get("side", "HOLD"), "—"
            )
            lines.append(
                f"<b>{sym}</b> {side_emoji}  strength={sig.get('strength', 0):.2f}\n"
                f"  {sig.get('extra', '')}"
            )
        lines.append(f"\n🕐 {_now_str()}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_pause(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if self._trader is None:
            await update.message.reply_text("⚠️ Bot 未绑定交易器。")
            return
        self._trader.risk.global_state.halted = True
        self._trader.risk.global_state.halt_reason = "tg_manual_pause"
        logger.warning("TG 指令：手动暂停交易")
        await update.message.reply_text(
            "⏸️ <b>交易已暂停</b>\n当前轮次完成后不再开新仓。\n发送 /resume 恢复。",
            parse_mode="HTML",
        )

    async def _cmd_resume(self, update: Any, context: Any) -> None:
        self._update_chat_id(update)
        if self._trader is None:
            await update.message.reply_text("⚠️ Bot 未绑定交易器。")
            return
        self._trader.risk.reset_halt()
        logger.info("TG 指令：手动恢复交易")
        await update.message.reply_text("▶️ <b>交易已恢复</b>", parse_mode="HTML")

    # ─── 生命周期 ─────────────────────────────────────────────────

    async def start(self) -> None:
        """启动指令轮询（在后台 asyncio Task 中运行）。"""
        if not self.enabled:
            return
        (Bot, Update, ParseMode, TelegramError,
         Application, CommandHandler, ContextTypes) = _import_telegram()
        if Application is None:
            return

        try:
            self._app = Application.builder().token(self._token).build()
            self._app.add_handler(CommandHandler("start",   self._cmd_help))
            self._app.add_handler(CommandHandler("help",    self._cmd_help))
            self._app.add_handler(CommandHandler("myid",    self._cmd_myid))
            self._app.add_handler(CommandHandler("pos",     self._cmd_pos))
            self._app.add_handler(CommandHandler("equity",  self._cmd_equity))
            self._app.add_handler(CommandHandler("status",  self._cmd_status))
            self._app.add_handler(CommandHandler("signals", self._cmd_signals))
            self._app.add_handler(CommandHandler("pause",   self._cmd_pause))
            self._app.add_handler(CommandHandler("resume",  self._cmd_resume))

            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            # 统一使用 Application 内部的 bot，确保主动推送与指令回复共用同一连接
            self._bot = self._app.bot
            self._started = True
            logger.info("Telegram Bot 指令轮询已启动（/help 查看指令）")
        except Exception as e:  # noqa: BLE001
            logger.warning("Telegram Bot 启动失败（功能降级为仅推送）: {}", e)

    async def stop(self) -> None:
        """关闭指令轮询。"""
        if self._app and self._started:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                logger.info("Telegram Bot 已停止")
            except Exception as e:  # noqa: BLE001
                logger.debug("TG Bot 停止时异常（可忽略）: {}", e)
