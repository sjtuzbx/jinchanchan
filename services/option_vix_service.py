import datetime
import math
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from logger import Logger
from .constants import SINA_HEADERS


class OptionVixService:
    """Compute VIX-like metrics for CSI index families via ETF options."""

    TARGETS = {
        "IH": {
            "name": "上证50",
            "opt_code": "OP510050.SH",
            "exchange": "SSE",
            "index_code": "000016.SH",
        },
        "IF": {
            "name": "沪深300",
            "opt_code": "OP510300.SH",
            "exchange": "SSE",
            "index_code": "000300.SH",
        },
        "IC": {
            "name": "中证500",
            "opt_code": "OP510500.SH",
            "exchange": "SSE",
            "index_code": "000905.SH",
        },
        "IM": {
            "name": "中证1000",
            "opt_code": "OP588000.SH",
            "exchange": "SSE",
            "index_code": "000852.SH",
        },
    }

    TARGET_DAYS = 30

    def __init__(self, pro_client=None, session: Optional[requests.Session] = None, risk_free_rate: float = 0.02):
        self.pro = pro_client
        self.session = session or requests.Session()
        self.session.headers.update(SINA_HEADERS)
        self.risk_free_rate = risk_free_rate
        self._meta_cache: Dict[str, List[Dict]] = {}
        self._meta_timestamp: Dict[str, float] = {}

    def get_vix_snapshots(self) -> List[Dict]:
        if self.pro is None:
            return []
        today = datetime.date.today()
        payloads = []
        for symbol, meta in self.TARGETS.items():
            try:
                snapshot = self._build_snapshot(symbol, meta, today)
            except Exception as exc:
                Logger.error(f"build VIX snapshot failed for {symbol}: {exc}")
                snapshot = {
                    "symbol": symbol,
                    "index_name": meta["name"],
                    "index_code": meta["index_code"],
                    "error": "计算失败",
                }
            if snapshot:
                payloads.append(snapshot)
        return payloads

    def get_term_structure_snapshots(self) -> List[Dict]:
        if self.pro is None:
            return []
        today = datetime.date.today()
        payloads = []
        for symbol, meta in self.TARGETS.items():
            try:
                snapshot = self._build_term_structure(symbol, meta, today)
            except Exception as exc:
                Logger.error(f"build term structure failed for {symbol}: {exc}")
                snapshot = {
                    "symbol": symbol,
                    "index_name": meta["name"],
                    "index_code": meta["index_code"],
                    "error": "计算失败",
                }
            if snapshot:
                payloads.append(snapshot)
        return payloads

    def _build_snapshot(self, symbol: str, meta: Dict, today: datetime.date) -> Dict:
        metadata = self._get_metadata(symbol, meta)
        base_payload = {
            "symbol": symbol,
            "index_name": meta["name"],
            "index_code": meta["index_code"],
            "option_family": meta["opt_code"],
            "risk_free_rate": self.risk_free_rate,
            "target_days": self.TARGET_DAYS,
            "source": "Sina期权实时 + CBOE VIX模型",
        }
        if not metadata:
            base_payload["error"] = "缺少期权合约元数据"
            return base_payload

        future_contracts = [row for row in metadata if row["maturity"] >= today]
        if not future_contracts:
            base_payload["error"] = "暂无可用合约"
            return base_payload

        grouped: Dict[datetime.date, List[Dict]] = {}
        for row in future_contracts:
            grouped.setdefault(row["maturity"], []).append(row)

        ordered_terms = sorted(grouped.items(), key=lambda x: x[0])
        structured_terms: List[Tuple[datetime.date, int, List[Dict]]] = []
        for maturity, rows in ordered_terms:
            days = max((maturity - today).days, 0)
            if days <= 0:
                continue
            structured_terms.append((maturity, days, rows))

        if not structured_terms:
            base_payload["error"] = "找不到未来到期的合约"
            return base_payload

        eligible = [term for term in structured_terms if term[1] >= 7]
        if len(eligible) >= 2:
            terms_to_use = eligible[:2]
        elif len(eligible) == 1:
            extra = [term for term in structured_terms if term not in eligible]
            terms_to_use = [eligible[0]] + extra[:1]
        else:
            terms_to_use = structured_terms[:2]

        codes = []
        for _, _, rows in terms_to_use:
            codes.extend(row["ts_code_short"] for row in rows)
        quotes = self._fetch_option_quotes(codes)

        legs = []
        for maturity, days, rows in terms_to_use:
            leg = self._build_leg(rows, quotes, maturity, days)
            if leg:
                legs.append(leg)

        if not legs:
            base_payload["error"] = "缺少有效报价"
            return base_payload

        vix_value = self._combine_legs(legs)
        base_payload["legs"] = legs
        base_payload["timestamp"] = max((leg.get("timestamp") for leg in legs if leg.get("timestamp")), default=None)
        if vix_value is None:
            base_payload["error"] = "插值失败"
        else:
            base_payload["vix_value"] = round(vix_value, 2)
        return base_payload

    def _build_term_structure(self, symbol: str, meta: Dict, today: datetime.date) -> Dict:
        metadata = self._get_metadata(symbol, meta)
        base_payload = {
            "symbol": symbol,
            "index_name": meta["name"],
            "index_code": meta["index_code"],
            "option_family": meta["opt_code"],
            "risk_free_rate": self.risk_free_rate,
            "source": "Sina期权实时 · 平值期权隐含波动率",
        }
        if not metadata:
            base_payload["error"] = "缺少期权合约元数据"
            return base_payload

        future_contracts = [row for row in metadata if row["maturity"] >= today]
        if not future_contracts:
            base_payload["error"] = "暂无可用合约"
            return base_payload

        grouped: Dict[datetime.date, List[Dict]] = {}
        for row in future_contracts:
            grouped.setdefault(row["maturity"], []).append(row)

        ordered_terms = sorted(grouped.items(), key=lambda x: x[0])
        structured_terms = []
        for maturity, rows in ordered_terms:
            days = max((maturity - today).days, 0)
            if days <= 0:
                continue
            structured_terms.append((maturity, days, rows))

        if not structured_terms:
            base_payload["error"] = "找不到未来到期的合约"
            return base_payload

        codes = []
        for _, _, rows in structured_terms:
            codes.extend(row["ts_code_short"] for row in rows)
        quotes = self._fetch_option_quotes(codes)

        terms = []
        for maturity, days, rows in structured_terms:
            term = self._build_atm_term(rows, quotes, maturity, days)
            if term:
                terms.append(term)

        if not terms:
            base_payload["error"] = "缺少有效报价"
            return base_payload

        base_payload["terms"] = terms
        base_payload["timestamp"] = max((term.get("timestamp") for term in terms if term.get("timestamp")), default=None)
        return base_payload

    def _build_leg(self, rows: Sequence[Dict], quotes: Dict[str, Dict], maturity: datetime.date, days: int) -> Optional[Dict]:
        call_quotes: Dict[float, Dict] = {}
        put_quotes: Dict[float, Dict] = {}
        timestamps = []
        spot = None
        for row in rows:
            quote = quotes.get(row["ts_code_short"])
            if not quote:
                continue
            price = self._mid_price(quote)
            if price is None or price <= 0:
                continue
            strike = row["strike"]
            entry = {
                "price": price,
                "bid": quote["bid"],
                "ask": quote["ask"],
            }
            timestamps.append(quote.get("timestamp"))
            if spot is None and quote.get("underlying") is not None:
                spot = quote["underlying"]

            if row["call_put"] == "C":
                call_quotes[strike] = entry
            else:
                put_quotes[strike] = entry

        if not call_quotes or not put_quotes:
            return None

        T = days / 365.0
        forward = self._forward_price(call_quotes, put_quotes, T)
        if forward is None:
            return None
        strikes = sorted(set(call_quotes.keys()) | set(put_quotes.keys()))
        k0_candidates = [strike for strike in strikes if strike <= forward]
        if not k0_candidates:
            return None
        k0 = max(k0_candidates)

        q_map: Dict[float, float] = {}
        for strike in strikes:
            call = call_quotes.get(strike)
            put = put_quotes.get(strike)
            if strike == k0 and call and put:
                q_price = (call["price"] + put["price"]) / 2
            elif strike < k0 and put:
                q_price = put["price"]
            elif strike > k0 and call:
                q_price = call["price"]
            else:
                continue
            if q_price and q_price > 0:
                q_map[strike] = q_price

        usable_strikes = sorted(q_map.keys())
        if len(usable_strikes) < 3:
            return None

        sum_term = 0.0
        for idx, strike in enumerate(usable_strikes):
            if idx == 0:
                delta_k = usable_strikes[idx + 1] - strike
            elif idx == len(usable_strikes) - 1:
                delta_k = strike - usable_strikes[idx - 1]
            else:
                delta_k = (usable_strikes[idx + 1] - usable_strikes[idx - 1]) / 2
            sum_term += (delta_k / (strike * strike)) * q_map[strike]

        variance = (2 * math.exp(self.risk_free_rate * T) / T) * sum_term - (1 / T) * ((forward / k0 - 1) ** 2)
        if variance <= 0:
            return None

        return {
            "maturity": maturity.isoformat(),
            "days_to_expiry": days,
            "variance": variance,
            "sigma": round(math.sqrt(variance) * 100, 2),
            "forward": forward,
            "k0": k0,
            "strike_count": len(usable_strikes),
            "spot_price": spot,
            "timestamp": max((t for t in timestamps if t), default=None),
        }

    def _build_atm_term(self, rows: Sequence[Dict], quotes: Dict[str, Dict], maturity: datetime.date, days: int) -> Optional[Dict]:
        call_quotes: Dict[float, Dict] = {}
        put_quotes: Dict[float, Dict] = {}
        timestamps = []
        spot_prices = []
        for row in rows:
            quote = quotes.get(row["ts_code_short"])
            if not quote:
                continue
            price = self._mid_price(quote)
            if price is None or price <= 0:
                continue
            strike = row["strike"]
            entry = {
                "price": price,
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
            }
            if quote.get("underlying") is not None:
                spot_prices.append(quote["underlying"])
            timestamps.append(quote.get("timestamp"))
            if row["call_put"] == "C":
                call_quotes[strike] = entry
            else:
                put_quotes[strike] = entry

        if not call_quotes and not put_quotes:
            return None
        if not spot_prices:
            return None

        spot_prices = [x for x in spot_prices if x and x > 0]
        if not spot_prices:
            return None
        spot = sorted(spot_prices)[len(spot_prices) // 2]

        strikes = sorted(set(call_quotes.keys()) | set(put_quotes.keys()))
        if not strikes:
            return None
        atm_strike = min(strikes, key=lambda k: abs(k - spot))

        T = days / 365.0
        call_iv = None
        put_iv = None
        if atm_strike in call_quotes:
            call_iv = self._implied_volatility(
                price=call_quotes[atm_strike]["price"],
                spot=spot,
                strike=atm_strike,
                T=T,
                is_call=True,
            )
        if atm_strike in put_quotes:
            put_iv = self._implied_volatility(
                price=put_quotes[atm_strike]["price"],
                spot=spot,
                strike=atm_strike,
                T=T,
                is_call=False,
            )

        iv_values = [v for v in (call_iv, put_iv) if v is not None]
        if not iv_values:
            return None
        iv = sum(iv_values) / len(iv_values)
        return {
            "maturity": maturity.isoformat(),
            "days_to_expiry": days,
            "strike": atm_strike,
            "spot_price": spot,
            "call_iv": round(call_iv * 100, 2) if call_iv is not None else None,
            "put_iv": round(put_iv * 100, 2) if put_iv is not None else None,
            "iv": round(iv * 100, 2),
            "timestamp": max((t for t in timestamps if t), default=None),
        }

    def _combine_legs(self, legs: Sequence[Dict]) -> Optional[float]:
        if not legs:
            return None
        legs_sorted = sorted(legs, key=lambda x: x["days_to_expiry"])
        target_T = self.TARGET_DAYS / 365.0
        if len(legs_sorted) == 1:
            leg = legs_sorted[0]
            if leg["variance"] <= 0 or leg["days_to_expiry"] <= 0:
                return None
            variance = leg["variance"]
            T = leg["days_to_expiry"] / 365.0
            scaled = variance * (target_T / T)
            return math.sqrt(scaled) * 100

        first, second = legs_sorted[0], legs_sorted[1]
        T1 = first["days_to_expiry"] / 365.0
        T2 = second["days_to_expiry"] / 365.0
        if T1 == T2:
            return math.sqrt(first["variance"]) * 100

        if target_T <= T1:
            variance = first["variance"]
            scale = target_T / T1
            return math.sqrt(variance * scale) * 100
        if target_T >= T2:
            variance = second["variance"]
            scale = target_T / T2
            return math.sqrt(variance * scale) * 100

        weight1 = (T2 - target_T) / (T2 - T1)
        weight2 = 1 - weight1
        numerator = T1 * first["variance"] * weight1 + T2 * second["variance"] * weight2
        variance_target = numerator / target_T
        if variance_target <= 0:
            return None
        return math.sqrt(variance_target) * 100

    def _forward_price(self, calls: Dict[float, Dict], puts: Dict[float, Dict], T: float) -> Optional[float]:
        common = sorted(set(calls.keys()) & set(puts.keys()), key=lambda x: abs(calls[x]["price"] - puts[x]["price"]))
        if not common:
            return None
        strike = common[0]
        diff = calls[strike]["price"] - puts[strike]["price"]
        return strike + math.exp(self.risk_free_rate * T) * diff

    def _implied_volatility(
        self,
        price: float,
        spot: float,
        strike: float,
        T: float,
        is_call: bool,
        max_iter: int = 60,
    ) -> Optional[float]:
        if price <= 0 or spot <= 0 or strike <= 0 or T <= 0:
            return None
        intrinsic = max(spot - strike, 0) if is_call else max(strike - spot, 0)
        if price < intrinsic:
            return None

        low = 1e-4
        high = 5.0
        price_high = self._bs_price(spot, strike, T, high, is_call)
        if price_high is None:
            return None
        while price_high < price and high < 10.0:
            high *= 2
            price_high = self._bs_price(spot, strike, T, high, is_call)
            if price_high is None:
                return None

        for _ in range(max_iter):
            mid = (low + high) / 2
            mid_price = self._bs_price(spot, strike, T, mid, is_call)
            if mid_price is None:
                return None
            if abs(mid_price - price) < 1e-4:
                return mid
            if mid_price > price:
                high = mid
            else:
                low = mid
        return (low + high) / 2

    def _bs_price(self, spot: float, strike: float, T: float, sigma: float, is_call: bool) -> Optional[float]:
        if sigma <= 0 or T <= 0 or spot <= 0 or strike <= 0:
            return None
        sqrt_t = math.sqrt(T)
        d1 = (math.log(spot / strike) + (self.risk_free_rate + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        if is_call:
            return spot * self._norm_cdf(d1) - strike * math.exp(-self.risk_free_rate * T) * self._norm_cdf(d2)
        return strike * math.exp(-self.risk_free_rate * T) * self._norm_cdf(-d2) - spot * self._norm_cdf(-d1)

    def _norm_cdf(self, x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _mid_price(self, quote: Dict) -> Optional[float]:
        bid = quote.get("bid")
        ask = quote.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return bid if bid and bid > 0 else (ask if ask and ask > 0 else None)

    def _fetch_option_quotes(self, codes: Sequence[str]) -> Dict[str, Dict]:
        result: Dict[str, Dict] = {}
        if not codes:
            return result
        chunk_size = 60
        for idx in range(0, len(codes), chunk_size):
            chunk = codes[idx: idx + chunk_size]
            url = ",".join(f"CON_OP_{code}" for code in chunk)
            resp = self.session.get(f"https://hq.sinajs.cn/list={url}", timeout=5)
            resp.raise_for_status()
            for line in resp.text.strip().splitlines():
                match = re.match(r"var\s+hq_str_CON_OP_(\w+)=\"(.*)\";", line)
                if not match:
                    continue
                code, payload = match.groups()
                fields = payload.split(",")
                if len(fields) < 40:
                    continue
                result[code] = {
                    "bid": self._safe_float(self._value_at(fields, 22)),
                    "ask": self._safe_float(self._value_at(fields, 12)),
                    "underlying": self._safe_float(self._value_at(fields, 7)),
                    "timestamp": self._value_at(fields, 32),
                }
        return result

    def _get_metadata(self, symbol: str, meta: Dict) -> List[Dict]:
        cached = self._meta_cache.get(symbol)
        ts = self._meta_timestamp.get(symbol, 0)
        if cached and time.time() - ts < 3600:
            return cached

        try:
            df = self.pro.opt_basic(
                exchange=meta["exchange"],
                opt_code=meta["opt_code"],
                fields="ts_code,call_put,exercise_price,maturity_date",
            )
        except Exception as exc:
            Logger.error(f"load opt_basic failed for {symbol}: {exc}")
            return cached or []

        if df is None or df.empty:
            return cached or []

        records: List[Dict] = []
        for row in df.itertuples():
            try:
                ts_code = getattr(row, "ts_code")
                maturity = getattr(row, "maturity_date")
                strike = float(getattr(row, "exercise_price"))
                call_put = getattr(row, "call_put")
            except AttributeError:
                continue
            if not ts_code or not maturity or not strike:
                continue
            try:
                maturity_date = datetime.datetime.strptime(str(maturity), "%Y%m%d").date()
            except ValueError:
                continue
            records.append(
                {
                    "ts_code": ts_code,
                    "ts_code_short": str(ts_code).split(".")[0],
                    "call_put": call_put,
                    "strike": strike,
                    "maturity": maturity_date,
                }
            )
        self._meta_cache[symbol] = records
        self._meta_timestamp[symbol] = time.time()
        return records

    def _value_at(self, fields: Sequence[str], idx: int) -> Optional[str]:
        return fields[idx] if idx < len(fields) else None

    def _safe_float(self, value: Optional[str]) -> Optional[float]:
        try:
            if value in ("", None):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
