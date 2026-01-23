import datetime
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from logger import Logger


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


class AlphaPortfolioMonitor:
    def __init__(self, csv_path: Path, state_path: Path, base_date: str):
        self.csv_path = csv_path
        self.state_path = state_path
        self.base_date = base_date

    def refresh(self) -> Dict:
        weights, csv_hash, missing_symbols = self._load_weights()
        if not weights:
            return {"error": "alpha_pick.csv 无有效持仓"}

        state = self._load_state()
        if not state or state.get("csv_hash") != csv_hash:
            state = self._rebalance_or_init(state, weights, csv_hash, missing_symbols)

        state = self._update_nav(state)
        self._save_state(state)
        return self._build_response(state)

    def _load_weights(self) -> Tuple[List[Dict], str, List[str]]:
        if not self.csv_path.exists():
            return [], "", [str(self.csv_path)]
        raw = self.csv_path.read_bytes()
        csv_hash = hashlib.md5(raw).hexdigest()
        df = pd.read_csv(self.csv_path, header=None, encoding="utf-8-sig")
        if df.empty or df.shape[1] < 2:
            return [], csv_hash, []
        symbols = df.iloc[:, 1].astype(str).str.strip()
        weights = pd.to_numeric(df.iloc[:, -1], errors="coerce")
        data = []
        missing = []
        for symbol, weight in zip(symbols, weights):
            if not symbol or pd.isna(weight):
                continue
            yahoo_symbol = self._to_yahoo_symbol(symbol)
            if not yahoo_symbol:
                missing.append(symbol)
                continue
            data.append({"symbol": symbol, "yahoo_symbol": yahoo_symbol, "weight": float(weight) / 100.0})
        total = sum(item["weight"] for item in data)
        if total > 0:
            for item in data:
                item["weight"] = item["weight"] / total
        return data, csv_hash, missing

    def _to_yahoo_symbol(self, symbol: str) -> str:
        clean = symbol.strip()
        if not clean:
            return ""
        if " " in clean:
            clean = clean.replace(" ", "-")
        return clean

    def _load_state(self) -> Optional[Dict]:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text())
        except Exception as exc:
            Logger.error(f"load alpha portfolio state failed: {exc}")
            return None

    def _save_state(self, state: Dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False))

    def _rebalance_or_init(self, state: Optional[Dict], weights: List[Dict], csv_hash: str, missing_symbols: List[str]) -> Dict:
        nav_history = state.get("nav_history", []) if state else []
        if nav_history:
            current_nav = nav_history[-1]["nav"]
            reason = "weights_update"
            price_map, rebalance_date = self._fetch_latest_common_prices(weights)
            if not price_map:
                return {
                    "error": "无法获取行情数据",
                    "csv_hash": csv_hash,
                    "missing_symbols": missing_symbols,
                }
        else:
            current_nav = 1.0
            reason = "init"
            price_map, rebalance_date = self._fetch_base_prices(weights)
            if not price_map:
                return {
                    "error": "无法获取基准行情数据",
                    "csv_hash": csv_hash,
                    "missing_symbols": missing_symbols,
                }

        available = [item for item in weights if item["yahoo_symbol"] in price_map]
        missing_price = [item["symbol"] for item in weights if item["yahoo_symbol"] not in price_map]
        if missing_price:
            missing_symbols = sorted(set(missing_symbols + missing_price))
        total_weight = sum(item["weight"] for item in available)
        if total_weight <= 0:
            return {"error": "无法匹配持仓行情", "csv_hash": csv_hash}

        new_holdings = []
        for item in available:
            price_info = price_map.get(item["yahoo_symbol"])
            price = price_info["price"]
            if not price:
                continue
            adj_weight = item["weight"] / total_weight
            shares = current_nav * adj_weight / price
            new_holdings.append(
                {
                    "symbol": item["symbol"],
                    "yahoo_symbol": item["yahoo_symbol"],
                    "weight": adj_weight,
                    "shares": shares,
                    "base_price": price,
                    "base_price_date": price_info["date"],
                }
            )

        rebalance_logs = state.get("rebalance_logs", []) if state else []
        rebalance_logs.append(
            {
                "date": rebalance_date,
                "reason": reason,
                "holdings": [
                    {
                        "symbol": h["symbol"],
                        "weight": h["weight"],
                        "price": h["base_price"],
                        "shares": h["shares"],
                    }
                    for h in new_holdings
                ],
            }
        )

        if not nav_history:
            nav_history = [{"date": rebalance_date, "nav": current_nav}]

        return {
            "base_date": self.base_date,
            "csv_hash": csv_hash,
            "holdings": new_holdings,
            "nav_history": nav_history,
            "rebalance_logs": rebalance_logs,
            "missing_symbols": missing_symbols,
        }

    def _update_nav(self, state: Dict) -> Dict:
        holdings = state.get("holdings", [])
        if not holdings:
            return state
        weights = [{"yahoo_symbol": h["yahoo_symbol"]} for h in holdings]
        price_map, latest_date = self._fetch_latest_common_prices(weights)
        if not price_map:
            return state
        latest_prices = {}
        for item in holdings:
            info = price_map.get(item["yahoo_symbol"])
            if info:
                latest_prices[item["symbol"]] = info

        nav = 0.0
        for item in holdings:
            info = price_map.get(item["yahoo_symbol"])
            if not info:
                continue
            nav += item["shares"] * info["price"]

        nav_history = state.get("nav_history", [])
        if nav_history and nav_history[-1]["date"] == latest_date:
            state["latest_prices"] = latest_prices
            return state

        nav_history.append({"date": latest_date, "nav": nav})
        state["nav_history"] = nav_history
        state["latest_prices"] = latest_prices
        return state

    def _fetch_latest_common_prices(self, items: List[Dict]) -> Tuple[Dict[str, Dict], str]:
        today = datetime.date.today()
        start = today - datetime.timedelta(days=12)
        end = today + datetime.timedelta(days=1)
        histories: Dict[str, Dict[str, float]] = {}
        for item in items:
            symbol = item["yahoo_symbol"]
            history = self._fetch_history(symbol, start, end)
            if not history:
                continue
            histories[symbol] = history

        if not histories:
            return {}, ""

        common_dates = None
        for history in histories.values():
            dates = set(history.keys())
            common_dates = dates if common_dates is None else common_dates.intersection(dates)
        if not common_dates:
            return {}, ""
        latest_date = max(common_dates)
        price_map: Dict[str, Dict] = {}
        for symbol, history in histories.items():
            price_map[symbol] = {"date": latest_date, "price": history[latest_date]}
        return price_map, latest_date

    def _fetch_base_prices(self, items: List[Dict]) -> Tuple[Dict[str, Dict], str]:
        try:
            target = datetime.datetime.strptime(self.base_date, "%Y-%m-%d").date()
        except Exception:
            target = datetime.date.today()
        today = datetime.date.today()
        end = max(target + datetime.timedelta(days=1), today + datetime.timedelta(days=1))
        start = target - datetime.timedelta(days=10)
        price_map: Dict[str, Dict] = {}
        base_dates: List[str] = []
        for item in items:
            symbol = item["yahoo_symbol"]
            history = self._fetch_history(symbol, start, end)
            if not history:
                continue
            date, price = self._close_on_or_before(history, target)
            if not date:
                date, price = self._latest_from_history(history)
            if not date:
                continue
            price_map[symbol] = {"date": date, "price": price}
            base_dates.append(date)
        base_date = max(base_dates) if base_dates else ""
        return price_map, base_date

    def _fetch_history(self, symbol: str, start: datetime.date, end: datetime.date) -> Dict[str, float]:
        params = {
            "period1": int(datetime.datetime.combine(start, datetime.time.min).timestamp()),
            "period2": int(datetime.datetime.combine(end, datetime.time.min).timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
        try:
            resp = requests.get(
                YAHOO_CHART_URL.format(symbol=symbol),
                headers=YAHOO_HEADERS,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            Logger.error(f"fetch yahoo history failed: {symbol}, err={exc}")
            return {}

        try:
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        except Exception:
            return {}

        history: Dict[str, float] = {}
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            dt = datetime.datetime.utcfromtimestamp(ts).date()
            history[dt.strftime("%Y-%m-%d")] = float(close)
        return history

    def _latest_from_history(self, history: Dict[str, float]) -> Tuple[str, Optional[float]]:
        if not history:
            return "", None
        latest_date = max(history.keys())
        return latest_date, history[latest_date]

    def _close_on_or_before(self, history: Dict[str, float], target: datetime.date) -> Tuple[str, Optional[float]]:
        candidates = [d for d in history.keys() if d <= target.strftime("%Y-%m-%d")]
        if not candidates:
            return "", None
        date = max(candidates)
        return date, history[date]

    def _build_response(self, state: Dict) -> Dict:
        nav_history = state.get("nav_history", [])
        current_nav = nav_history[-1]["nav"] if nav_history else None
        base_nav = nav_history[0]["nav"] if nav_history else None
        ret = (current_nav / base_nav - 1) if current_nav is not None and base_nav else None
        holdings = []
        latest_prices = state.get("latest_prices", {})
        for h in state.get("holdings", []):
            latest = latest_prices.get(h["symbol"], {})
            latest_price = latest.get("price")
            latest_date = latest.get("date")
            value = h["shares"] * latest_price if latest_price is not None else None
            pnl = (latest_price - h["base_price"]) * h["shares"] if latest_price is not None else None
            holdings.append(
                {
                    **h,
                    "latest_price": latest_price,
                    "latest_date": latest_date,
                    "value": value,
                    "pnl": pnl,
                }
            )
        return {
            "base_date": state.get("base_date"),
            "current_nav": current_nav,
            "base_nav": base_nav,
            "return_pct": ret,
            "nav_history": nav_history,
            "holdings": holdings,
            "rebalance_logs": state.get("rebalance_logs", []),
            "missing_symbols": state.get("missing_symbols", []),
        }
