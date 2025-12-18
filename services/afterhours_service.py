import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

from logger import Logger
from services.utils import beijing_now


class AfterHoursHistoryStore:
    """Persist after-hours indicators for quick reuse."""

    def __init__(self, cache_file: Path, max_days: int = 370):
        self.cache_file = cache_file
        self.max_days = max_days
        self.records: List[Dict] = []
        self._load()

    def _load(self):
        if not self.cache_file.exists():
            self.records = []
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                self.records = payload[-self.max_days :]
            else:
                self.records = []
        except Exception:
            self.records = []

    def _persist(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self.records[-self.max_days :], ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, record: Dict):
        trade_date = record.get("trade_date")
        if not trade_date:
            return
        self.records = [r for r in self.records if r.get("trade_date") != trade_date]
        self.records.append(record)
        self.records = sorted(self.records, key=lambda r: r.get("trade_date"))[-self.max_days :]
        self._persist()

    def get_history(self) -> List[Dict]:
        return list(self.records)

    def get_latest(self) -> Optional[Dict]:
        if not self.records:
            return None
        return self.records[-1]

    def has_date(self, trade_date: str) -> bool:
        return any(r.get("trade_date") == trade_date for r in self.records)


class AfterHoursService:
    """Compute after-hours turnover and financing indicators."""

    def __init__(self, pro_client, cache_file: Path):
        self.pro = pro_client
        self.store = AfterHoursHistoryStore(cache_file)
        self._trade_day_cache: Dict[str, bool] = {}

    def get_payload(self) -> Dict:
        trade_date = self._determine_target_trade_date()
        if trade_date and not self.store.has_date(trade_date):
            record = self._compute_record(trade_date)
            if record:
                self.store.upsert(record)
        history = self.store.get_history()
        latest = history[-1] if history else None
        return {
            "target_trade_date": trade_date,
            "latest": latest,
            "history": history[-250:],
        }

    def _compute_record(self, trade_date: str) -> Optional[Dict]:
        try:
            daily_basic = self.pro.daily_basic(trade_date=trade_date, fields="ts_code,total_mv")
            daily = self.pro.daily(trade_date=trade_date, fields="ts_code,amount")
            margin = self.pro.margin(trade_date=trade_date, fields="exchange_id,rzmre")
        except Exception as exc:
            Logger.error(f"after-hours fetch failed for {trade_date}: {exc}")
            return None

        if daily_basic is None or daily_basic.empty or daily is None or daily.empty:
            return None

        total_mv = float(daily_basic["total_mv"].dropna().sum()) * 1e4  # 万元 -> 元
        turnover_amount = float(daily["amount"].dropna().sum()) * 1e3  # 千元 -> 元
        turnover_ratio = turnover_amount / total_mv if total_mv else None

        margin_buy = 0.0
        if margin is not None and not margin.empty and "rzmre" in margin.columns:
            margin_buy = float(margin["rzmre"].dropna().sum())
        margin_ratio = margin_buy / turnover_amount if turnover_amount else None

        record = {
            "trade_date": trade_date,
            "total_market_value": total_mv,
            "turnover_amount": turnover_amount,
            "turnover_ratio": turnover_ratio,
            "margin_buy_amount": margin_buy,
            "margin_ratio": margin_ratio,
        }
        return record

    def _determine_target_trade_date(self) -> Optional[str]:
        now = beijing_now()
        base_date = now.date() if now.hour >= 17 else now.date() - datetime.timedelta(days=1)
        for offset in range(10):
            candidate = base_date - datetime.timedelta(days=offset)
            date_str = candidate.strftime("%Y%m%d")
            if self._is_trade_day(date_str):
                return date_str
        return None

    def _is_trade_day(self, date_str: str) -> bool:
        if date_str in self._trade_day_cache:
            return self._trade_day_cache[date_str]
        try:
            cal = self.pro.trade_cal(exchange="SSE", start_date=date_str, end_date=date_str)
            is_open = bool(cal is not None and not cal.empty and int(cal.iloc[0]["is_open"]) == 1)
        except Exception:
            is_open = False
        self._trade_day_cache[date_str] = is_open
        return is_open
