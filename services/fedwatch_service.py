import calendar
import csv
import datetime as dt
import io
import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from logger import Logger
from services.utils import beijing_now


class FedWatchService:
    """Estimate FedWatch-style probabilities via public data."""

    FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    TARGET_LOWER_SERIES = "DFEDTARL"
    TARGET_UPPER_SERIES = "DFEDTARU"
    FUTURES_URL = "https://query1.finance.yahoo.com/v8/finance/chart/ZQ=F"
    MEETING_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

    def __init__(self, cache_file: Path, calendar_file: Path, history_file: Path):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                "Referer": "https://finance.yahoo.com/quote/ZQ=F",
            }
        )
        self.cache_file = cache_file
        self.calendar_file = calendar_file
        self.history_file = history_file
        self._cache: Optional[Dict] = self._load_json(cache_file)
        self._cache_expire = 0.0
        self._calendar = self._load_json(calendar_file) or {}

    def get_snapshot(self) -> Dict:
        now = time.time()
        if self._cache and now < self._cache_expire:
            return self._cache

        target_range = self._fetch_target_range()
        if not target_range:
            return {"error": "无法获取当前目标利率区间"}

        meeting = self._resolve_next_meeting()
        futures_history = self._fetch_futures_history()
        if not meeting or not futures_history:
            snapshot = {"error": "FedWatch 数据暂不可用"}
            self._cache = snapshot
            self._cache_expire = now + 120
            return snapshot

        inferred = self._build_inferred_rates(meeting, target_range, futures_history)
        prob_rows = self._build_probability_rows(target_range, inferred)

        snapshot = {
            "note": "基于FRED目标区间 & Yahoo Fed Funds期货估算",
            "current_lower": target_range["lower"],
            "current_upper": target_range["upper"],
            "current_mid": target_range["mid"],
            "target_updated_at": target_range.get("updated_at"),
            "meeting": meeting,
            "implied_rates": inferred,
            "probabilities": prob_rows,
        }

        self._cache = snapshot
        self._cache_expire = now + 300
        self._persist_json(self.cache_file, snapshot)
        return snapshot

    # ----------------------- data collection helpers ----------------------- #

    def _fetch_target_range(self) -> Optional[Dict]:
        lower = self._fetch_fred_latest(self.TARGET_LOWER_SERIES)
        upper = self._fetch_fred_latest(self.TARGET_UPPER_SERIES)
        if not lower or not upper:
            return None
        lower_date, lower_val = lower
        upper_date, upper_val = upper
        if lower_val is None or upper_val is None:
            return None
        mid = (lower_val + upper_val) / 2.0
        updated_at = max(lower_date or "", upper_date or "")
        return {
            "lower": lower_val,
            "upper": upper_val,
            "mid": mid,
            "updated_at": updated_at,
        }

    def _fetch_fred_latest(self, series: str) -> Optional[Tuple[Optional[str], Optional[float]]]:
        url = self.FRED_URL.format(series=series)
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            Logger.error(f"fetch FRED {series} failed: {exc}")
            return None
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        if len(rows) <= 1:
            return None
        for row in reversed(rows[1:]):
            if len(row) < 2:
                continue
            date_str = row[0].strip()
            value_str = row[1].strip()
            if not date_str or not value_str:
                continue
            try:
                return date_str, float(value_str)
            except ValueError:
                continue
        return None

    def _fetch_futures_history(self) -> List[Dict]:
        params = {"range": "1mo", "interval": "1d"}
        try:
            resp = self.session.get(self.FUTURES_URL, params=params, timeout=8)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            Logger.error(f"fetch fed funds futures failed: {exc}")
            return []

        try:
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        except Exception as exc:
            Logger.error(f"parse futures history failed: {exc}")
            return []

        history: List[Dict] = []
        for ts_val, price in zip(timestamps, closes):
            if price in (None, 0):
                continue
            date = dt.datetime.utcfromtimestamp(ts_val).date()
            implied_rate = 100.0 - float(price)
            history.append({
                "trade_date": date.strftime("%Y%m%d"),
                "implied_rate": implied_rate,
            })
        history = sorted({rec["trade_date"]: rec for rec in history}.values(), key=lambda x: x["trade_date"])
        if history:
            self._persist_json(self.history_file, history[-40:])
        return history

    # -------------------------- meeting schedule -------------------------- #

    def _resolve_next_meeting(self) -> Optional[Dict]:
        today = beijing_now().date()
        meetings = self._calendar.get("meetings") or []
        meetings = [m for m in meetings if m.get("date")]
        meetings.sort(key=lambda m: m["date"])
        for item in meetings:
            try:
                meeting_date = dt.datetime.strptime(item["date"], "%Y%m%d").date()
            except Exception:
                continue
            if meeting_date >= today:
                return {"date": item["date"], "label": item.get("label")}
        # schedule may be stale, attempt refresh
        self._refresh_calendar()
        meetings = self._calendar.get("meetings") or []
        meetings.sort(key=lambda m: m["date"])
        for item in meetings:
            try:
                meeting_date = dt.datetime.strptime(item["date"], "%Y%m%d").date()
            except Exception:
                continue
            if meeting_date >= today:
                return {"date": item["date"], "label": item.get("label")}
        # fallback: assume next meeting in 45 days
        fallback_date = today + dt.timedelta(days=45)
        return {"date": fallback_date.strftime("%Y%m%d"), "label": fallback_date.strftime("%Y-%m-%d"), "estimated": True}

    def _refresh_calendar(self):
        try:
            resp = requests.get(self.MEETING_CALENDAR_URL, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            Logger.error(f"fetch FOMC calendar failed: {exc}")
            return
        soup = BeautifulSoup(resp.text, "html.parser")
        meetings: List[Dict] = []
        for panel in soup.select("div.panel.panel-default"):
            heading = panel.find("h4")
            if not heading:
                continue
            text = heading.get_text(strip=True)
            if "FOMC Meetings" not in text:
                continue
            try:
                year = int([token for token in text.split() if token.isdigit()][0])
            except Exception:
                continue
            for row in panel.select("div.fomc-meeting"):
                month_el = row.select_one(".fomc-meeting__month")
                date_el = row.select_one(".fomc-meeting__date")
                if not month_el or not date_el:
                    continue
                raw_month = month_el.get_text(strip=True)
                raw_date = date_el.get_text(strip=True)
                if "notation" in raw_date.lower():
                    continue
                try:
                    meeting_date = self._parse_meeting_date(year, raw_month, raw_date)
                except Exception:
                    continue
                meetings.append({
                    "date": meeting_date.strftime("%Y%m%d"),
                    "label": f"{meeting_date.strftime('%Y-%m-%d')} ({raw_month} {raw_date})",
                })
        if meetings:
            self._calendar = {"meetings": sorted(meetings, key=lambda m: m["date"]), "fetched_at": beijing_now().strftime("%Y-%m-%d")}
            self._persist_json(self.calendar_file, self._calendar)

    def _parse_meeting_date(self, year: int, month_text: str, date_text: str) -> dt.date:
        month_label = month_text.split('/')[-1].strip()
        month_map = {
            "January": 1,
            "February": 2,
            "March": 3,
            "April": 4,
            "Apr": 4,
            "May": 5,
            "June": 6,
            "July": 7,
            "August": 8,
            "September": 9,
            "October": 10,
            "November": 11,
            "December": 12,
        }
        month = month_map.get(month_label, month_map.get(month_label.title(), 1))
        clean_date = date_text.split()[0].replace('*', '').strip()
        if '-' in clean_date:
            parts = [p for p in clean_date.split('-') if p]
            day_str = parts[-1]
        else:
            day_str = ''.join(filter(str.isdigit, clean_date))
        day = int(day_str)
        # handle wrap like "30-1" meaning next month
        if '-' in clean_date and int(parts[0]) > day:
            month += 1
            if month > 12:
                month = 1
                year += 1
        return dt.date(year, month, day)

    # ---------------- probability construction ----------------- #

    def _build_inferred_rates(self, meeting: Dict, target: Dict, history: List[Dict]) -> Dict:
        meeting_date = dt.datetime.strptime(meeting["date"], "%Y%m%d").date()
        context = self._meeting_context(meeting_date)
        latest = history[-1]
        day_offset = self._find_history_offset(history, 1)
        week_offset = self._find_history_offset(history, 5)
        inferred = {}
        inferred["latest"] = self._calc_expected_rate(latest["implied_rate"], target["mid"], context)
        if day_offset:
            inferred["prev_day"] = self._calc_expected_rate(day_offset["implied_rate"], target["mid"], context)
        if week_offset:
            inferred["prev_week"] = self._calc_expected_rate(week_offset["implied_rate"], target["mid"], context)
        inferred["latest_date"] = latest["trade_date"]
        if day_offset:
            inferred["prev_day_date"] = day_offset["trade_date"]
        if week_offset:
            inferred["prev_week_date"] = week_offset["trade_date"]
        return inferred

    def _meeting_context(self, meeting_date: dt.date) -> Dict:
        first_day = meeting_date.replace(day=1)
        days_in_month = calendar.monthrange(meeting_date.year, meeting_date.month)[1]
        pre_days = (meeting_date - first_day).days
        pre_days = max(0, min(pre_days, days_in_month - 1))
        post_days = max(1, days_in_month - pre_days)
        return {
            "pre_days": pre_days,
            "post_days": post_days,
            "days_in_month": days_in_month,
        }

    def _calc_expected_rate(self, implied_rate: float, current_mid: float, ctx: Dict) -> float:
        total = ctx["days_in_month"]
        pre = ctx["pre_days"]
        post = ctx["post_days"]
        expected_new = (implied_rate * total - current_mid * pre) / max(post, 1)
        return max(0.0, min(10.0, expected_new))

    def _find_history_offset(self, history: List[Dict], business_days: int) -> Optional[Dict]:
        if not history:
            return None
        target = dt.datetime.strptime(history[-1]["trade_date"], "%Y%m%d").date()
        needed = business_days
        idx = len(history) - 2
        while idx >= 0 and needed > 0:
            prev_date = dt.datetime.strptime(history[idx]["trade_date"], "%Y%m%d").date()
            delta = (target - prev_date).days
            if delta >= needed:
                return history[idx]
            idx -= 1
        return history[0] if history else None

    def _build_probability_rows(self, target: Dict, inferred: Dict) -> List[Dict]:
        latest = inferred.get("latest")
        if latest is None:
            return []
        reference_rates = [val for key, val in inferred.items() if key.endswith("date") is False and isinstance(val, (int, float))]
        lowers = self._build_grid(reference_rates, target)
        today_dist = self._distribution_for_rate(latest, lowers)
        day_dist = self._distribution_for_rate(inferred.get("prev_day"), lowers)
        week_dist = self._distribution_for_rate(inferred.get("prev_week"), lowers)

        rows: List[Dict] = []
        for lower in lowers:
            prob = today_dist.get(lower, 0.0)
            day_prob = day_dist.get(lower, 0.0) if day_dist else None
            week_prob = week_dist.get(lower, 0.0) if week_dist else None
            if prob < 0.003 and (day_prob or 0) < 0.003 and (week_prob or 0) < 0.003:
                continue
            rows.append(
                {
                    "lower": lower,
                    "upper": round(lower + 0.25, 4),
                    "prob": prob,
                    "delta_1d": None if day_prob is None else prob - day_prob,
                    "delta_1w": None if week_prob is None else prob - week_prob,
                }
            )
        rows.sort(key=lambda r: r["prob"], reverse=True)
        return rows[:8]

    def _build_grid(self, reference_rates: List[float], target: Dict) -> List[float]:
        if not reference_rates:
            reference_rates = [target["mid"]]
        min_rate = min(reference_rates) - 0.75
        max_rate = max(reference_rates) + 0.75
        min_lower = max(0.0, math.floor(min_rate / 0.25) * 0.25)
        max_lower = max(0.25, math.ceil(max_rate / 0.25) * 0.25)
        lowers = []
        val = min_lower
        while val <= max_lower:
            lowers.append(round(val, 4))
            val = round(val + 0.25, 4)
        return lowers

    def _distribution_for_rate(self, expected: Optional[float], lowers: List[float]) -> Dict[float, float]:
        if expected is None:
            return {}
        sigma = 0.18
        weights: Dict[float, float] = {}
        for lower in lowers:
            center = lower + 0.125
            weight = math.exp(-0.5 * ((center - expected) / sigma) ** 2)
            weights[lower] = weight
        total = sum(weights.values())
        if total <= 0:
            return {}
        return {lower: weight / total for lower, weight in weights.items()}

    # ---------------------------- persistence ---------------------------- #

    def _load_json(self, path: Path) -> Optional[Dict]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _persist_json(self, path: Path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            Logger.error(f"persist {path.name} failed: {exc}")
