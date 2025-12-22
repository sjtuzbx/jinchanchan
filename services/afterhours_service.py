import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

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

    HISTORY_DAYS = 260

    def __init__(self, pro_client, cache_file: Path):
        self.pro = pro_client
        self.store = AfterHoursHistoryStore(cache_file)
        self._trade_day_cache: Dict[str, bool] = {}

    def get_payload(self) -> Dict:
        trade_date = self._determine_target_trade_date()
        if trade_date:
            self._ensure_history(trade_date)
        last_known = self.store.get_latest()
        if trade_date and not self.store.has_date(trade_date):
            record = self._compute_record(trade_date)
            record = self._apply_margin_fallback(record, last_known)
            if record:
                self.store.upsert(record)
                last_known = record
        history = self.store.get_history()
        latest = history[-1] if history else None
        if latest and latest.get("margin_ratio") in (None, 0):
            refreshed = self._refresh_margin_for_date(latest.get("trade_date"))
            if refreshed:
                history = self.store.get_history()
                latest = history[-1] if history else None
        three_month_max = self._calc_three_month_max_turnover(history)
        return {
            "target_trade_date": (latest or {}).get("trade_date", trade_date),
            "requested_trade_date": trade_date,
            "latest": latest,
            "history": history[-250:],
            "three_month_max_turnover": three_month_max,
        }

    def _ensure_history(self, upto_date: str):
        history = self.store.get_history()
        existing = {rec.get("trade_date") for rec in history}
        if len(existing) >= self.HISTORY_DAYS and upto_date in existing:
            return
        try:
            upto_dt = datetime.datetime.strptime(upto_date, "%Y%m%d").date()
        except ValueError:
            return
        start_dt = upto_dt - datetime.timedelta(days=self.HISTORY_DAYS * 2)
        try:
            cal = self.pro.trade_cal(
                exchange="SSE",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=upto_date,
                is_open="1",
            )
        except Exception as exc:
            Logger.error(f"after-hours trade cal failed: {exc}")
            return
        if cal is None or cal.empty:
            return
        calendar_days = cal["cal_date"].astype(str).tolist()
        target_dates = [d for d in calendar_days if d <= upto_date][-self.HISTORY_DAYS :]
        missing = [d for d in target_dates if d not in existing]
        if not missing:
            return
        start_range = missing[0]
        end_range = missing[-1]
        mv_map, turnover_map, margin_map = self._bulk_collect(start_range, end_range)
        history_sorted = sorted(history, key=lambda r: r.get("trade_date"))
        last_known = history_sorted[-1] if history_sorted else None
        for date in missing:
            record = self._build_record_from_maps(date, mv_map, turnover_map, margin_map)
            record = self._apply_margin_fallback(record, last_known)
            if record is None and last_known:
                record = {
                    **{k: v for k, v in last_known.items() if k != "trade_date"},
                    "trade_date": date,
                    "derived": True,
                }
            record = self._apply_margin_fallback(record, last_known)
            if record:
                self.store.upsert(record)
                last_known = record

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

    def _bulk_collect(self, start: str, end: str):
        mv_map: Dict[str, float] = {}
        turnover_map: Dict[str, float] = {}
        margin_map: Dict[str, float] = {}
        try:
            basic = self.pro.daily_basic(start_date=start, end_date=end, fields="trade_date,total_mv")
            if basic is not None and not basic.empty:
                mv_map = (basic.groupby("trade_date")["total_mv"].sum() * 1e4).to_dict()
        except Exception as exc:
            Logger.error(f"bulk daily_basic failed: {exc}")
        try:
            daily = self.pro.daily(start_date=start, end_date=end, fields="trade_date,amount")
            if daily is not None and not daily.empty:
                turnover_map = (daily.groupby("trade_date")["amount"].sum() * 1e3).to_dict()
        except Exception as exc:
            Logger.error(f"bulk daily failed: {exc}")
        try:
            margin = self.pro.margin(start_date=start, end_date=end, fields="trade_date,rzmre")
            if margin is not None and not margin.empty:
                margin_map = margin.groupby("trade_date")["rzmre"].sum().to_dict()
        except Exception as exc:
            Logger.error(f"bulk margin failed: {exc}")
        return mv_map, turnover_map, margin_map

    def _build_record_from_maps(self, trade_date: str, mv_map: Dict[str, float], turnover_map: Dict[str, float], margin_map: Dict[str, float]) -> Optional[Dict]:
        total_mv = mv_map.get(trade_date)
        turnover_amount = turnover_map.get(trade_date)
        if total_mv in (None, 0) or turnover_amount in (None, 0):
            return None
        margin_buy = margin_map.get(trade_date, 0.0)
        return {
            "trade_date": trade_date,
            "total_market_value": float(total_mv),
            "turnover_amount": float(turnover_amount),
            "turnover_ratio": float(turnover_amount) / float(total_mv) if total_mv else None,
            "margin_buy_amount": float(margin_buy),
            "margin_ratio": float(margin_buy) / float(turnover_amount) if turnover_amount else None,
        }

    def _apply_margin_fallback(self, record: Optional[Dict], fallback: Optional[Dict]) -> Optional[Dict]:
        if not record:
            return record
        ratio = record.get("margin_ratio")
        if ratio not in (None, 0):
            return record
        if not fallback:
            return record
        if not fallback.get("margin_ratio"):
            return record
        combined = dict(record)
        combined["margin_buy_amount"] = fallback.get("margin_buy_amount")
        combined["margin_ratio"] = fallback.get("margin_ratio")
        combined["margin_from"] = fallback.get("trade_date")
        combined["margin_derived"] = True
        return combined

    def _determine_target_trade_date(self) -> Optional[str]:
        now = beijing_now()
        # 09:00 前仍展示前一交易日之前的数据，09:00 后切换到最新的上一交易日
        base_date = now.date() - datetime.timedelta(days=1)
        if now.hour < 9:
            base_date -= datetime.timedelta(days=1)
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

    def _calc_three_month_max_turnover(self, history: List[Dict]) -> Optional[Dict]:
        if not history:
            return None
        latest = history[-1]
        latest_date = latest.get("trade_date")
        if not latest_date:
            return None
        try:
            latest_dt = datetime.datetime.strptime(str(latest_date), "%Y%m%d").date()
        except ValueError:
            return None
        start_dt = latest_dt - datetime.timedelta(days=90)
        max_amount = None
        max_date = None
        for row in history:
            trade_date = row.get("trade_date")
            amount = row.get("turnover_amount")
            if trade_date is None or amount is None:
                continue
            try:
                trade_dt = datetime.datetime.strptime(str(trade_date), "%Y%m%d").date()
            except ValueError:
                continue
            if trade_dt < start_dt:
                continue
            if max_amount is None or amount > max_amount:
                max_amount = amount
                max_date = trade_date
        if max_amount is None:
            return None
        return {"trade_date": max_date, "turnover_amount": max_amount}

    def _refresh_margin_for_date(self, trade_date: Optional[str]) -> bool:
        if not trade_date:
            return False
        try:
            margin = self.pro.margin(trade_date=trade_date, fields="exchange_id,rzmre")
        except Exception as exc:
            Logger.error(f"refresh margin failed for {trade_date}: {exc}")
            return False
        if margin is None or margin.empty or "rzmre" not in margin.columns:
            return False
        margin_buy = float(margin["rzmre"].dropna().sum())
        history = self.store.get_history()
        record = next((r for r in history if r.get("trade_date") == trade_date), None)
        if not record:
            return False
        turnover = record.get("turnover_amount")
        ratio = margin_buy / turnover if turnover else None
        updated = dict(record)
        updated["margin_buy_amount"] = margin_buy
        updated["margin_ratio"] = ratio
        updated.pop("margin_from", None)
        updated.pop("margin_derived", None)
        self.store.upsert(updated)
        return True
