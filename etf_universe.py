"""Fixed, independent universe for the US ETF Daily DXDX radar."""
from __future__ import annotations


ETF_UNIVERSE = (
    "SPY",
    "QQQ",
    "DIA",
    "IWM",
    "XLK",
    "SOXX",
    "SMH",
    "XLC",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLB",
    "XLRE",
)

ETF_NAMES = {
    "SPY": "标普500ETF",
    "QQQ": "纳斯达克100ETF",
    "DIA": "道琼斯ETF",
    "IWM": "罗素2000ETF",
    "XLK": "科技ETF",
    "SOXX": "半导体ETF-iShares",
    "SMH": "半导体ETF-VanEck",
    "XLC": "通信服务ETF",
    "XLF": "金融ETF",
    "XLE": "能源ETF",
    "XLV": "医疗ETF",
    "XLI": "工业ETF",
    "XLY": "可选消费ETF",
    "XLP": "必选消费ETF",
    "XLU": "公用事业ETF",
    "XLB": "材料ETF",
    "XLRE": "房地产ETF",
}


def is_etf_symbol(symbol: str) -> bool:
    """Accept only the explicitly audited ETF whitelist."""
    return (symbol or "").strip().upper() in ETF_NAMES


def get_etf_universe() -> list[str]:
    return list(ETF_UNIVERSE)


def etf_name(symbol: str) -> str:
    return ETF_NAMES[symbol.upper()]
