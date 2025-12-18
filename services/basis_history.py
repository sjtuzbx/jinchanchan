import datetime
import json
from bisect import bisect_right
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from logger import Logger


class BasisHistory:
    """Cache yearly basis statistics for CSI index futures main contracts."""

    SYMBOL_INDEX = {
        "IF": "000300.SH",
        "IH": "000016.SH",
        "IC": "000905.SH",
        "IM": "000852.SH",
    }

    def __init__(self, pro, cache_file: Path, lookback_years: int = 5):
        self.pro = pro
        self.cache_file = cache_file
        self.lookback_years = lookback_years
        self.data: Dict[str, List[Dict[str, float]]] = {}
        self._sorted_values: Dict[str, List[float]] = {}

    def ensure_latest(self, force: bool = False):
        today = datetime.date.today().strftime("%Y%m%d")
        if not self.cache_file.exists():
            Logger.info("basis history cache missing, rebuilding...")
            self.refresh()
            return
        if force:
            self.refresh()
            return

        try:
            payload = json.loads(self.cache_file.read_text())
            last_date = payload.get("end_date")
            if not last_date or last_date < today:
                Logger.info("basis history cache stale, refreshing...")
                self.refresh()
                return
            self.data = payload.get("symbols", {})
            self._rebuild_sorted()
        except Exception as exc:
            Logger.error(f"load basis history cache failed: {exc}")
            self.refresh()

    def refresh(self):
        start = (datetime.date.today() - datetime.timedelta(days=365 * self.lookback_years)).strftime("%Y%m%d")
        end = datetime.date.today().strftime("%Y%m%d")
        payload = {"start_date": start, "end_date": end, "generated_at": datetime.datetime.now().isoformat(), "symbols": {}}
        for symbol in self.SYMBOL_INDEX:
            try:
                records = self._build_symbol_history(symbol, start, end)
                payload["symbols"][symbol] = records
                Logger.info(f"basis history refreshed for {symbol}, rows={len(records)}")
            except Exception as exc:
                Logger.error(f"refresh basis history for {symbol} failed: {exc}")
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=False))
        self.data = payload["symbols"]
        self._rebuild_sorted()

    def get_percentile(self, symbol: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        values = self._sorted_values.get(symbol)
        if not values:
            return None
        idx = bisect_right(values, value)
        pct = idx / len(values) * 100
        return round(pct, 2)

    def _rebuild_sorted(self):
        self._sorted_values = {}
        for symbol, rows in self.data.items():
            arr = sorted(r["annualized_basis"] for r in rows if r.get("annualized_basis") is not None)
            if arr:
                self._sorted_values[symbol] = arr

    def _build_symbol_history(self, symbol: str, start: str, end: str) -> List[Dict[str, float]]:
        mapping = self.pro.fut_mapping(ts_code=f"{symbol}.CFX", start_date=start, end_date=end)
        if mapping is None or mapping.empty:
            return []
        mapping = mapping.sort_values("trade_date")

        unique_contracts = mapping["mapping_ts_code"].dropna().unique().tolist()
        futures_frames = []
        for code in unique_contracts:
            try:
                df = self.pro.fut_daily(ts_code=code, start_date=start, end_date=end, fields="ts_code,trade_date,close")
            except Exception as exc:
                Logger.error(f"load fut_daily {code} failed: {exc}")
                continue
            if df is None or df.empty:
                continue
            futures_frames.append(df[["ts_code", "trade_date", "close"]])

        if not futures_frames:
            return []

        futures_df = pd.concat(futures_frames, ignore_index=True)
        futures_df["trade_date"] = futures_df["trade_date"].astype(str)
        futures_df["ts_code"] = futures_df["ts_code"].astype(str)
        futures_map = futures_df.set_index(["ts_code", "trade_date"])["close"].to_dict()

        index_code = self.SYMBOL_INDEX[symbol]
        index_df = self.pro.index_daily(ts_code=index_code, start_date=start, end_date=end)
        if index_df is None or index_df.empty:
            return []
        spot_map = dict(zip(index_df["trade_date"].astype(str), index_df["close"]))

        records: List[Dict[str, float]] = []
        for _, row in mapping.iterrows():
            trade_date = str(row["trade_date"])
            contract_code = row["mapping_ts_code"]
            fut_price = futures_map.get((contract_code, trade_date))
            spot_price = spot_map.get(trade_date)
            if fut_price in (None, 0) or spot_price in (None, 0):
                continue

            expiry_date = self._contract_expiry(contract_code)
            trade_dt = datetime.datetime.strptime(trade_date, "%Y%m%d").date()
            days_left = (expiry_date - trade_dt).days
            if days_left <= 0:
                continue

            basis_pct = (fut_price - spot_price) / spot_price * 100
            annualized_basis = basis_pct * (365 / days_left)
            records.append(
                {
                    "trade_date": trade_date,
                    "annualized_basis": round(annualized_basis, 6),
                }
            )
        return records

    def _contract_expiry(self, contract_code: str) -> datetime.date:
        clean = contract_code.split(".")[0]
        year = 2000 + int(clean[2:4])
        month = int(clean[4:6])
        first_day = datetime.date(year, month, 1)
        days_to_friday = (4 - first_day.weekday()) % 7
        first_friday = first_day + datetime.timedelta(days=days_to_friday)
        third_friday = first_friday + datetime.timedelta(days=14)
        return third_friday
