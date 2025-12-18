import json
from pathlib import Path
from typing import Dict, List, Optional


class VixHistoryStore:
    """Persist daily implied volatility closes for last N days."""

    def __init__(self, cache_file: Path, max_days: int = 370):
        self.cache_file = cache_file
        self.max_days = max_days
        self.data: Dict[str, List[Dict]] = {}
        self._load()

    def _load(self):
        if not self.cache_file.exists():
            self.data = {}
            return
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.data = {
                    symbol: [
                        {"date": str(item.get("date")), "value": float(item.get("value"))}
                        for item in entries
                        if item.get("date") and item.get("value") is not None
                    ]
                    for symbol, entries in payload.items()
                }
            else:
                self.data = {}
        except Exception:
            self.data = {}

    def _persist(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(self, trade_date: str, snapshots: List[Dict], force: bool = False):
        if not trade_date:
            return
        changed = False
        for snap in snapshots or []:
            symbol = snap.get("symbol")
            value = snap.get("vix_value")
            if symbol is None or value is None:
                continue
            entries = self.data.setdefault(symbol, [])
            if entries and entries[-1]["date"] == trade_date and not force:
                continue
            entries = [entry for entry in entries if entry["date"] != trade_date]
            entries.append({"date": trade_date, "value": round(float(value), 4)})
            if len(entries) > self.max_days:
                entries = entries[-self.max_days :]
            self.data[symbol] = entries
            changed = True
        if changed:
            self._persist()

    def get_history(self) -> Dict[str, List[Dict]]:
        return self.data

    def latest_date(self, symbol: str) -> Optional[str]:
        entries = self.data.get(symbol)
        if not entries:
            return None
        return entries[-1]["date"]
