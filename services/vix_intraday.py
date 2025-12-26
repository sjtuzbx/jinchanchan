import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .utils import beijing_now


@dataclass
class IntradaySeries:
    date: str = ""
    points: Dict[str, List[Dict]] = field(default_factory=dict)


class VixIntradayStore:
    """Persist intraday VIX snapshots and aggregate into K-line data."""

    def __init__(self, cache_file: Path, interval_minutes: int = 5, max_points: int = 1500):
        self.cache_file = cache_file
        self.interval_minutes = interval_minutes
        self.max_points = max_points
        self.data = IntradaySeries()
        self._load()

    def _load(self):
        if not self.cache_file.exists():
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        date = payload.get("date")
        points = payload.get("points")
        if not isinstance(date, str) or not isinstance(points, dict):
            return
        cleaned: Dict[str, List[Dict]] = {}
        for symbol, items in points.items():
            if not isinstance(items, list):
                continue
            cleaned[symbol] = [
                {"time": str(item.get("time")), "value": float(item.get("value"))}
                for item in items
                if item.get("time") and item.get("value") is not None
            ]
        self.data = IntradaySeries(date=date, points=cleaned)

    def _persist(self):
        payload = {"date": self.data.date, "points": self.data.points}
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(self, snapshots: List[Dict], timestamp: Optional[str] = None):
        now = beijing_now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")
        record_dt = now
        if timestamp:
            try:
                parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=now.tzinfo)
                parsed = parsed.astimezone(now.tzinfo)
                record_dt = parsed
                date_str = parsed.strftime("%Y-%m-%d")
                time_str = parsed.strftime("%H:%M:%S")
            except Exception:
                pass

        if not self._is_trading_session(record_dt.time()):
            return

        if self.data.date != date_str:
            self.data = IntradaySeries(date=date_str, points={})

        changed = False
        for snap in snapshots or []:
            symbol = snap.get("symbol")
            value = snap.get("vix_value")
            if not symbol or value is None:
                continue
            items = self.data.points.setdefault(symbol, [])
            if items and items[-1]["time"] == time_str:
                if items[-1]["value"] != float(value):
                    items[-1]["value"] = float(value)
                    changed = True
                continue
            items.append({"time": time_str, "value": float(value)})
            if len(items) > self.max_points:
                self.data.points[symbol] = items[-self.max_points :]
            changed = True

        if changed:
            self._persist()

    def _is_trading_session(self, t: datetime.time) -> bool:
        morning_start = datetime.time(9, 30)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(13, 0)
        afternoon_end = datetime.time(15, 0)
        in_morning = morning_start <= t <= morning_end
        in_afternoon = afternoon_start <= t <= afternoon_end
        return in_morning or in_afternoon

    def get_kline(self, interval_minutes: Optional[int] = None) -> Dict:
        series: Dict[str, List[Dict]] = {}
        for symbol, items in (self.data.points or {}).items():
            series[symbol] = [
                {"t": item.get("time"), "v": item.get("value")}
                for item in items
                if item.get("time") and item.get("value") is not None
            ]
        return {"date": self.data.date, "interval": None, "series": series}
