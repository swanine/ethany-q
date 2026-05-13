"""
配置模块（config.py）
==================
集中管理 API Key、交易对、杠杆、仓位模式、风控阈值等。
支持从环境变量 / .env 加载；生产环境切勿将密钥写入代码仓库。

说明：pydantic-settings 对「列表类型」的环境变量会先走 json.loads；
若 .env 里写「逗号分隔」或留空，会触发 JSONDecodeError。
因此交易对使用字符串字段 `trading_symbols_raw`，再解析为列表。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Dict, List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SYMBOLS_STR = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,SUIUSDT"


class MarginMode(str, Enum):
    """仓位保证金模式（与币安 marginType 对齐）。"""

    ISOLATED = "ISOLATED"  # 逐仓
    CROSSED = "CROSSED"  # 全仓


class AppConfig(BaseSettings):
    """
    应用级配置：通过环境变量覆盖默认值。
    变量名与 .env.example 保持一致，便于运维与容器化部署。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # —— API（环境变量名：BINANCE_API_KEY / BINANCE_API_SECRET / BINANCE_TESTNET）—— #
    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_testnet: bool = Field(default=False)

    # —— 交易标的：环境变量 TRADING_SYMBOLS 为「逗号分隔」或 JSON 数组字符串 —— #
    trading_symbols_raw: str = Field(
        default=_DEFAULT_SYMBOLS_STR,
        validation_alias=AliasChoices("TRADING_SYMBOLS", "trading_symbols"),
    )

    # 默认杠杆（每个标的可在运行时按 symbol 覆盖）
    default_leverage: int = Field(default=5, ge=1, le=125)

    # 逐仓 / 全仓
    margin_mode: MarginMode = Field(default=MarginMode.CROSSED)

    # 最大持仓占「总权益」比例（名义价值层面粗粒度上限，具体下单仍由风控模块计算）
    max_portfolio_exposure_pct: float = Field(
        default=0.8,
        ge=0.01,
        le=1.0,
    )

    # —— 全局风控 —— #
    max_drawdown_pct: float = Field(default=0.15, ge=0.01, le=0.99)
    max_risk_per_trade_pct: float = Field(
        default=0.015,
        ge=0.0001,
        le=0.05,
    )
    max_symbol_exposure_pct: float = Field(
        default=0.25,
        ge=0.01,
        le=1.0,
    )
    max_open_positions: int = Field(default=3, ge=1, le=50)

    # —— 各标的仓位权重 ——
    # 格式：JSON 字典，如 {"BTCUSDT":1.625,"ETHUSDT":1.625,"SOLUSDT":0.583}
    # 权重建议归一化（均值≈1.0），开仓数量 = 基础仓位 × 权重
    # 未配置的标的默认权重 1.0
    symbol_weights_raw: str = Field(
        default="",
        validation_alias=AliasChoices("SYMBOL_WEIGHTS", "symbol_weights"),
    )

    # —— Telegram 通知 —— #
    tg_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    )
    tg_chat_id: str = Field(
        default="",
        validation_alias=AliasChoices("TG_CHAT_ID", "TELEGRAM_CHAT_ID"),
    )
    # 推送级别：all=全部  order=仅开平仓  alert=仅告警  off=关闭
    tg_notify_level: str = Field(
        default="all",
        validation_alias=AliasChoices("TG_NOTIFY_LEVEL", "TELEGRAM_NOTIFY_LEVEL"),
    )

    # —— 执行 / 网络 —— #
    rest_recv_window: int = Field(
        default=5000,
        validation_alias=AliasChoices("REST_RECV_WINDOW", "BINANCE_RECV_WINDOW"),
    )
    ws_reconnect_delay_sec: float = Field(default=2.0)
    rest_max_retries: int = Field(default=5)

    # —— 回测默认 —— #
    backtest_taker_fee_rate: float = Field(default=0.0004)
    backtest_slippage_pct: float = Field(default=0.0002)

    @field_validator("trading_symbols_raw", mode="before")
    @classmethod
    def parse_trading_symbols_raw(cls, v):
        """空值、列表（少见）统一成可 split 的字符串。"""
        if v is None:
            return _DEFAULT_SYMBOLS_STR
        if isinstance(v, list):
            return ",".join(str(x).strip().upper() for x in v if str(x).strip())
        s = str(v).strip()
        if not s:
            return _DEFAULT_SYMBOLS_STR
        return s

    @staticmethod
    def _split_symbols(raw: str) -> List[str]:
        s = raw.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                return [str(x).strip().upper() for x in arr if str(x).strip()]
            except json.JSONDecodeError:
                pass
        parts = [p.strip().upper() for p in s.split(",") if p.strip()]
        return parts or _DEFAULT_SYMBOLS_STR.split(",")

    @property
    def trading_symbols(self) -> List[str]:
        """解析后的交易对列表。"""
        return self._split_symbols(self.trading_symbols_raw)

    @property
    def symbols(self) -> List[str]:
        """兼容旧字段名：对外仍用 symbols 指代交易对列表。"""
        return list(self.trading_symbols)

    @property
    def symbol_weights(self) -> Dict[str, float]:
        """解析后的各标的权重字典。空值时返回空字典（默认全部权重=1.0）。"""
        raw = (self.symbol_weights_raw or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).upper(): float(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return {}

    def get_symbol_weight(self, symbol: str) -> float:
        """返回指定标的的仓位权重，未配置时默认 1.0。"""
        return self.symbol_weights.get(symbol.upper(), 1.0)

    @property
    def futures_base_url(self) -> str:
        """REST 根地址。"""
        if self.binance_testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"

    @property
    def futures_ws_base(self) -> str:
        """
        合约行情 WS 根（组合流）。
        - 主网：wss://fstream.binance.com
        - 测试网：wss://fstream.binancefuture.com（注意前缀 fstream，非 stream）
        """
        if self.binance_testnet:
            return "wss://fstream.binancefuture.com"
        return "wss://fstream.binance.com"

    def validate_live_keys(self) -> None:
        """实盘启动前校验密钥已配置。"""
        if not self.binance_api_key or not self.binance_api_secret:
            raise ValueError("BINANCE_API_KEY / BINANCE_API_SECRET 未设置，无法连接实盘。")


def load_config() -> AppConfig:
    """加载配置单例（每次调用重新读取环境，便于测试）。"""
    return AppConfig()
