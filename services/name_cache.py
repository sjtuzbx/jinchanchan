import threading
from pathlib import Path
import pandas as pd
from backtest.data_manager import change_ts, change_dragon
from services.utils import load_json_if_exists, dump_json
from logger import Logger


class StockNameCache:
    """使用 Tushare 数据构建本地股票代码->名称映射"""

    def __init__(self, pro_client, cache_file=None):
        self._pro = pro_client
        self.cache_file = Path(cache_file or Path(__file__).resolve().parents[1] / "data" / "stock_names.json")
        self._cache = {}
        self._loaded = False
        self._lock = threading.Lock()

    def ensure_cache(self):
        with self._lock:
            if not self._loaded:
                cached = load_json_if_exists(self.cache_file, default={})
                if cached:
                    self._cache = cached
                self._loaded = True
        if not self._cache:
            self.refresh_full()

    def refresh_full(self):
        if self._pro is None:
            Logger.error("Tushare client unavailable, cannot refresh name cache")
            return
        Logger.info("Refreshing stock name cache from Tushare...")
        frames = []
        for status in ("L", "D", "P"):
            try:
                df = self._pro.stock_basic(list_status=status, fields="ts_code,name")
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception as ex:
                Logger.error(f"stock_basic failed for status {status}: {ex}")
        if not frames:
            return
        merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"])
        cache = {}
        for _, row in merged.iterrows():
            ts_code = str(row["ts_code"])
            name = str(row["name"])
            cache[ts_code] = name
            normalized = change_dragon(ts_code)
            cache[normalized] = name
        with self._lock:
            self._cache.update(cache)
            dump_json(self.cache_file, self._cache)
            Logger.info(f"Cached {len(self._cache)} entries -> {self.cache_file}")

    def get_name(self, code):
        if not code:
            return code
        self.ensure_cache()
        code = str(code)
        if code in self._cache:
            return self._cache[code]
        ts_code = change_ts(code)
        if ts_code in self._cache:
            return self._cache[ts_code]
        if self._pro is None:
            return code
        try:
            df = self._pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
            if df is not None and not df.empty:
                name = str(df.iloc[0]["name"])
                with self._lock:
                    self._cache[code] = name
                    self._cache[ts_code] = name
                    dump_json(self.cache_file, self._cache)
                return name
        except Exception as ex:
            Logger.error(f"stock name fetch failed for {code}: {ex}")
        return code

    def enrich_result(self, result):
        if not isinstance(result, dict):
            return result
        self.ensure_cache()
        for entry in result.get('position_history', []):
            for pos in entry.get('positions', []):
                code = pos.get('code')
                if code:
                    pos['name'] = self.get_name(code)
        for trade in result.get('trade_history', []):
            code = trade.get('code')
            if code:
                trade['name'] = self.get_name(code)
        for trade in result.get('trade_returns', []):
            code = trade.get('code')
            if code:
                trade['name'] = self.get_name(code)
        return result
