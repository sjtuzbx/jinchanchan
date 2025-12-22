import datetime
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
import time

from logger import Logger
from .constants import SINA_HEADERS
from .option_vix_service import OptionVixService


@dataclass
class ContractQuote:
    symbol: str
    contract: str
    last_price: Optional[float]
    open_price: Optional[float]
    high_price: Optional[float]
    low_price: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    expiry_date: datetime.date
    days_to_expiry: int
    basis: Optional[float]
    basis_pct: Optional[float]
    annualized_basis: Optional[float]
    daily_basis: Optional[float]


class FuturesMonitorService:
    """Fetch real-time futures/spot quotes and compute basis metrics."""

    SYMBOL_META = {
        "IF": {"name": "沪深300期货", "index": "sh000300", "ts_index": "000300.SH"},
        "IH": {"name": "上证50期货", "index": "sh000016", "ts_index": "000016.SH"},
        "IC": {"name": "中证500期货", "index": "sh000905", "ts_index": "000905.SH"},
        "IM": {"name": "中证1000期货", "index": "sh000852", "ts_index": "000852.SH"},
    }

    def __init__(self, basis_history=None, pro_client=None):
        self.session = requests.Session()
        self.session.headers.update(SINA_HEADERS)
        self.basis_history = basis_history
        self.pro = pro_client
        self._vix_cache = {"timestamp": 0, "data": []}
        self._term_cache = {"timestamp": 0, "data": []}
        self.vix_service = OptionVixService(pro_client=pro_client, session=self.session)

    def get_monitor_payload(self) -> Dict:
        today = datetime.date.today()
        symbol_contracts: Dict[str, List[str]] = {}
        all_future_codes: List[str] = []
        for symbol in self.SYMBOL_META:
            contracts = self._build_contract_codes(today, symbol)
            symbol_contracts[symbol] = contracts
            all_future_codes.extend([f"CFF_{code}" for code in contracts])

        future_quotes = self._fetch_quotes(all_future_codes)
        index_codes = [meta["index"] for meta in self.SYMBOL_META.values()]
        index_quotes = self._fetch_quotes(index_codes)
        index_payload = {
            symbol: self._parse_index_quote(meta["index"], index_quotes.get(meta["index"]))
            for symbol, meta in self.SYMBOL_META.items()
        }

        items = []
        for symbol, contracts in symbol_contracts.items():
            spot_info = index_payload.get(symbol) or {}
            spot_price = spot_info.get("price")
            contract_rows = []
            for contract in contracts:
                key = f"CFF_{contract}"
                fields = future_quotes.get(key)
                if not fields:
                    continue
                quote = self._parse_contract_quote(
                    symbol=symbol,
                    contract=contract,
                    fields=fields,
                    spot_price=spot_price,
                    today=today,
                )
                contract_rows.append(quote)

            items.append(
                {
                    "symbol": symbol,
                    "name": self.SYMBOL_META[symbol]["name"],
                    "spot_price": spot_price,
                    "spot_change": spot_info.get("change"),
                    "spot_change_pct": spot_info.get("change_pct"),
                    "index_code": self.SYMBOL_META[symbol]["index"],
                    "spot_detail": spot_info,
                    "contracts": [self._serialize_contract(symbol, row) for row in contract_rows],
                }
            )

        return {
            "generated_at": datetime.datetime.now().isoformat(),
            "items": items,
            "vix": self._get_vix_data(),
            "option_term_structure": self._get_term_structure(),
        }

    def _build_contract_codes(self, today: datetime.date, symbol: str) -> List[str]:
        """Return front month, next month, and next two quarter contracts."""
        year, month = today.year, today.month
        months = [
            (year, month),
            self._shift_month(year, month, 1),
        ]
        q1 = self._next_quarter(months[-1][0], months[-1][1])
        q2 = self._next_quarter(q1[0], q1[1])
        months.extend([q1, q2])

        seen = set()
        codes = []
        for y, m in months:
            key = (y, m)
            if key in seen:
                continue
            seen.add(key)
            codes.append(f"{symbol}{y % 100:02d}{m:02d}")
        return codes

    def _shift_month(self, year: int, month: int, delta: int) -> (int, int):
        total = year * 12 + month - 1 + delta
        new_year = total // 12
        new_month = total % 12 + 1
        return new_year, new_month

    def _next_quarter(self, year: int, month: int) -> (int, int):
        quarter_months = [3, 6, 9, 12]
        for qm in quarter_months:
            if month < qm:
                return year, qm
        return year + 1, 3

    def _fetch_quotes(self, codes: List[str]) -> Dict[str, List[str]]:
        if not codes:
            return {}
        url = f"https://hq.sinajs.cn/list={','.join(codes)}"
        resp = self.session.get(url, timeout=5)
        resp.raise_for_status()
        result = {}
        for line in resp.text.strip().splitlines():
            match = re.match(r"var\s+hq_str_(\w+)=\"(.*)\";", line)
            if not match:
                continue
            key, payload = match.groups()
            result[key] = payload.split(",")
        return result

    def _parse_index_quote(self, code: str, fields: Optional[List[str]]) -> Dict:
        if not fields:
            return {"code": code}

        def _flt(idx):
            return self._safe_float(self._value_at(fields, idx))

        prev_close = _flt(2)
        last_price = _flt(3)
        change = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change = last_price - prev_close
            change_pct = change / prev_close * 100
        return {
            "code": code,
            "name": self._value_at(fields, 0) or code.upper(),
            "open": _flt(1),
            "prev_close": prev_close,
            "price": last_price,
            "high": _flt(4),
            "low": _flt(5),
            "volume": self._safe_float(self._value_at(fields, 8)),
            "amount": self._safe_float(self._value_at(fields, 9)),
            "time": self._value_at(fields, 31),
            "date": self._value_at(fields, 30),
            "change": change,
            "change_pct": change_pct,
        }

    def _parse_contract_quote(
        self,
        symbol: str,
        contract: str,
        fields: List[str],
        spot_price: Optional[float],
        today: datetime.date,
    ) -> ContractQuote:
        # 新浪字段: 1=当日最高, 2=当日最低, 3=最新价, 13=昨结算
        last_price = self._safe_float(self._value_at(fields, 3)) or self._safe_float(self._value_at(fields, 0))
        prev_settle = self._safe_float(self._value_at(fields, 13)) or self._safe_float(self._value_at(fields, 0))
        change = None
        change_pct = None
        if last_price is not None and prev_settle not in (None, 0):
            change = last_price - prev_settle
            change_pct = change / prev_settle * 100

        expiry_date = self._contract_expiry(contract)
        days_left = max((expiry_date - today).days, 0)
        basis = None
        basis_pct = None
        annualized = None
        daily = None
        if last_price is not None and spot_price not in (None, 0):
            basis = last_price - spot_price
            basis_pct = basis / spot_price * 100
            if days_left > 0:
                annualized = basis_pct * (365 / days_left)
                daily = basis / days_left
            else:
                annualized = None
                daily = basis

        return ContractQuote(
            symbol=symbol,
            contract=contract,
            last_price=last_price,
            open_price=self._safe_float(self._value_at(fields, 0)),
            high_price=self._safe_float(self._value_at(fields, 1)),
            low_price=self._safe_float(self._value_at(fields, 2)),
            change=change,
            change_pct=change_pct,
            expiry_date=expiry_date,
            days_to_expiry=days_left,
            basis=basis,
            basis_pct=basis_pct,
            annualized_basis=annualized,
            daily_basis=daily,
        )

    def _contract_expiry(self, contract: str) -> datetime.date:
        year = 2000 + int(contract[2:4])
        month = int(contract[4:6])
        first_day = datetime.date(year, month, 1)
        days_to_friday = (4 - first_day.weekday()) % 7
        first_friday = first_day + datetime.timedelta(days=days_to_friday)
        third_friday = first_friday + datetime.timedelta(days=14)
        return third_friday

    def _serialize_contract(self, symbol: str, quote: ContractQuote) -> Dict:
        data = {
            "code": quote.contract,
            "last_price": quote.last_price,
            "open_price": quote.open_price,
            "high_price": quote.high_price,
            "low_price": quote.low_price,
            "change": quote.change,
            "change_pct": quote.change_pct,
            "expiry_date": quote.expiry_date.isoformat(),
            "days_to_expiry": quote.days_to_expiry,
            "basis": quote.basis,
            "basis_pct": quote.basis_pct,
            "annualized_basis": quote.annualized_basis,
            "daily_basis": quote.daily_basis,
        }
        if self.basis_history:
            data["annualized_percentile"] = self.basis_history.get_percentile(symbol, quote.annualized_basis)
        return data

    def _get_vix_data(self) -> List[Dict]:
        if self.pro is None or self.vix_service is None:
            return []
        now = time.time()
        if self._vix_cache["timestamp"] and now - self._vix_cache["timestamp"] < 60:
            return self._vix_cache["data"]
        try:
            data = self.vix_service.get_vix_snapshots()
        except Exception as exc:
            Logger.error(f"option VIX calc failed: {exc}")
            data = []
        self._vix_cache = {"timestamp": now, "data": data}
        return data

    def _get_term_structure(self) -> List[Dict]:
        if self.pro is None or self.vix_service is None:
            return []
        now = time.time()
        if self._term_cache["timestamp"] and now - self._term_cache["timestamp"] < 60:
            return self._term_cache["data"]
        try:
            data = self.vix_service.get_term_structure_snapshots()
        except Exception as exc:
            Logger.error(f"option term structure failed: {exc}")
            data = []
        self._term_cache = {"timestamp": now, "data": data}
        return data

    def _value_at(self, fields: List[str], idx: int) -> Optional[str]:
        return fields[idx] if idx < len(fields) else None

    def _safe_float(self, value) -> Optional[float]:
        try:
            if value in ("", None, "--"):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
