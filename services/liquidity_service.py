import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from logger import Logger
from services.utils import beijing_now, dump_json, load_json_if_exists


class LiquidityService:
    """Fetch and cache USD net liquidity data from FRED (daily, no resampling)."""

    SERIES = {
        "walcl": "WALCL",       # Millions
        "tga": "WTREGEN",       # Millions
        "rrp": "RRPONTSYD",     # Billions
    }

    def __init__(self, cache_file: Path, history_days: int = 60):
        self.cache_file = cache_file
        self.history_days = history_days

    def get_payload(self) -> Dict:
        now = beijing_now()
        cached = load_json_if_exists(self.cache_file, default=None)
        if cached and cached.get("history"):
            cached_latest = self._cached_latest_date(cached)
            if cached_latest:
                try:
                    if not self._has_new_data_since(cached_latest):
                        return cached
                except Exception as exc:
                    Logger.error(f"liquidity freshness check failed: {exc}")
        try:
            payload = self._build_payload(now)
            dump_json(self.cache_file, payload)
            return payload
        except Exception as exc:
            Logger.error(f"liquidity fetch failed: {exc}")
            if cached:
                cached["stale"] = True
                cached["error"] = "拉取流动性数据失败，展示缓存数据"
                return cached
            return {"error": "拉取流动性数据失败", "stale": True}

    def _build_payload(self, now: datetime.datetime) -> Dict:
        cutoff = now.date() - datetime.timedelta(days=max(self.history_days * 6, 420))
        walcl = self._load_series(self.SERIES["walcl"], cutoff) / 1000.0  # -> Billions
        tga = self._load_series(self.SERIES["tga"], cutoff) / 1000.0      # -> Billions
        rrp = self._load_series(self.SERIES["rrp"], cutoff)               # -> Billions

        df = pd.concat([walcl, tga, rrp], axis=1)
        df.columns = ["walcl_bil", "tga_bil", "rrp_bil"]
        df["net_liquidity_bil"] = df["walcl_bil"] - df["tga_bil"] - df["rrp_bil"]

        def latest_point(series: pd.Series) -> Optional[Dict]:
            series = series.dropna()
            if series.empty:
                return None
            idx = series.index[-1]
            return {"date": idx.strftime("%Y-%m-%d"), "value": float(series.iloc[-1])}

        components = {
            "walcl": latest_point(df["walcl_bil"]),
            "tga": latest_point(df["tga_bil"]),
            "rrp": latest_point(df["rrp_bil"]),
        }
        net_latest = latest_point(df["net_liquidity_bil"])
        net_series = df["net_liquidity_bil"].dropna().tail(self.history_days)
        history = [
            {"date": idx.strftime("%Y-%m-%d"), "net_liquidity_bil": float(val)}
            for idx, val in net_series.items()
        ]
        return {
            "updated_date": now.date().isoformat(),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S CST"),
            "components": components,
            "latest": net_latest,
            "history": history,
            "unit": "USD Billions",
            "source": "FRED",
        }

    def _load_series(self, series_id: str, cutoff_date: datetime.date) -> pd.Series:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        df = pd.read_csv(url)
        date_col = "DATE" if "DATE" in df.columns else "observation_date"
        df[date_col] = pd.to_datetime(df[date_col])
        series = pd.to_numeric(df[series_id], errors="coerce")
        series.index = df[date_col]
        series = series.sort_index()
        series = series[series.index.date >= cutoff_date]
        return series

    def _cached_latest_date(self, cached: Dict) -> Optional[datetime.date]:
        dates = []
        for key in ("walcl", "tga", "rrp"):
            date_str = cached.get("components", {}).get(key, {}).get("date")
            if date_str:
                try:
                    dates.append(datetime.date.fromisoformat(date_str))
                except ValueError:
                    pass
        latest_date = cached.get("latest", {}).get("date")
        if latest_date:
            try:
                dates.append(datetime.date.fromisoformat(latest_date))
            except ValueError:
                pass
        return max(dates) if dates else None

    def _has_new_data_since(self, cached_latest: datetime.date) -> bool:
        start = cached_latest - datetime.timedelta(days=7)
        for series_id in self.SERIES.values():
            latest = self._latest_observation_date(series_id, start)
            if latest and latest > cached_latest:
                return True
        return False

    def _latest_observation_date(self, series_id: str, start_date: datetime.date) -> Optional[datetime.date]:
        url = (
            "https://fred.stlouisfed.org/graph/fredgraph.csv"
            f"?id={series_id}&cosd={start_date.isoformat()}"
        )
        df = pd.read_csv(url)
        date_col = "DATE" if "DATE" in df.columns else "observation_date"
        df[date_col] = pd.to_datetime(df[date_col])
        values = pd.to_numeric(df[series_id], errors="coerce")
        df = df.loc[~values.isna(), date_col]
        if df.empty:
            return None
        return df.max().date()
