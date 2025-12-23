import csv
import datetime
import io
import json
import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from logger import Logger
from .constants import SINA_HEADERS


class MacroDataService:
    """Fetch macro indicators from freely accessible sources."""

    FRED_SERIES = {
        "VIXCLS": {"name": "美股 VIX 指数", "unit": "%", "multiplier": 1.0},
        "DGS30": {"name": "美国30年国债收益率", "unit": "%", "multiplier": 1.0},
        "DFII10": {"name": "TIPS 10年实际利率", "unit": "%", "multiplier": 1.0},
        "WALCL": {"name": "美联储资产负债表", "unit": "亿美元", "multiplier": 0.01},
        "WTREGEN": {"name": "TGA 账户余额", "unit": "亿美元", "multiplier": 0.01},
        "RRPONTSYD": {"name": "美股隔夜逆回购余额", "unit": "亿美元", "multiplier": 0.01},
    }

    INDEX_SERIES = {
        "sh000002": {"name": "全A指数", "source": "Sina"},
        "bj899050": {"name": "北证50", "source": "Sina"},
        "sz399640": {"name": "创业基础（微盘替代）", "source": "Sina"},
    }

    PLACEHOLDER_SERIES = [
        {"key": "CN30Y", "name": "中国30年国债收益率"},
        {"key": "PBOC_ASSETS", "name": "人民银行资产负债表"},
        {"key": "HKMA_BALANCE", "name": "香港银行间资金总结余"},
        {"key": "JISILU_CB", "name": "可转债等权指数"},
        {"key": "BEIJING_ALL", "name": "北证全指"},
        {"key": "MICROCAP_INDEX", "name": "微盘股指数"},
        {"key": "BB_CREDIT_IMPULSE_CN", "name": "中国-彭博信贷脉冲指数"},
    ]

    FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    SEP_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    SEP_TABLE_PATTERN = re.compile(r"fomcprojtabl(\d{8})\.htm", re.IGNORECASE)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(SINA_HEADERS)
        self._cache: Dict[str, Dict] = {}
        self._cache_expire = 0

    def get_macro_snapshot(self) -> List[Dict]:
        now = datetime.datetime.utcnow().timestamp()
        required_keys = {"DFII10", "SEP_REAL_RATE", "BB_CREDIT_IMPULSE_CN"}
        if self._cache_expire and now < self._cache_expire and self._cache:
            if required_keys.issubset(self._cache.keys()):
                return list(self._cache.values())

        snapshot: Dict[str, Dict] = {}
        snapshot.update(self._fetch_fred_series())
        snapshot.update(self._fetch_index_series())
        sep_real = self._fetch_sep_real_rate()
        if sep_real:
            snapshot[sep_real["key"]] = sep_real
        credit_impulse = self._fetch_bloomberg_credit_impulse()
        if credit_impulse:
            snapshot[credit_impulse["key"]] = credit_impulse
        for item in self.PLACEHOLDER_SERIES:
            if item["key"] in snapshot:
                continue
            snapshot[item["key"]] = {
                "key": item["key"],
                "name": item["name"],
                "error": "数据源待接入",
            }
        self._cache = snapshot
        self._cache_expire = now + 600
        return list(snapshot.values())

    def _fetch_fred_series(self) -> Dict[str, Dict]:
        results: Dict[str, Dict] = {}
        for series, meta in self.FRED_SERIES.items():
            try:
                url = self.FRED_URL.format(series=series)
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                date_str, value = self._parse_fred_csv(resp.text)
                if value is None:
                    raise ValueError("empty value")
                display_value = value * meta.get("multiplier", 1.0)
                results[series] = {
                    "key": series,
                    "name": meta["name"],
                    "value": display_value,
                    "unit": meta.get("unit"),
                    "updated_at": date_str,
                    "source": "FRED",
                }
            except Exception as exc:
                Logger.error(f"fetch FRED {series} failed: {exc}")
                results[series] = {
                    "key": series,
                    "name": meta["name"],
                    "error": "数据获取失败",
                }
        return results

    def _parse_fred_csv(self, text: str) -> (Optional[str], Optional[float]):
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) <= 1:
            return None, None
        for row in reversed(rows[1:]):
            if len(row) < 2:
                continue
            date_str, value_str = row[0].strip(), row[1].strip()
            if not date_str or not value_str:
                continue
            try:
                value = float(value_str)
            except ValueError:
                continue
            return date_str, value
        return None, None

    def _fetch_index_series(self) -> Dict[str, Dict]:
        if not self.INDEX_SERIES:
            return {}
        try:
            codes = ",".join(self.INDEX_SERIES.keys())
            resp = self.session.get(f"https://hq.sinajs.cn/list={codes}", timeout=5)
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
        except Exception as exc:
            Logger.error(f"fetch index series failed: {exc}")
            return {
                key: {"key": key, "name": meta["name"], "error": "行情获取失败"}
                for key, meta in self.INDEX_SERIES.items()
            }

        payloads: Dict[str, List[str]] = {}
        for line in lines:
            if not line:
                continue
            parts = line.split("=")
            if len(parts) != 2:
                continue
            key_part = parts[0].split("_")[-1]
            payload = parts[1].strip('";\n')
            payloads[key_part] = payload.split(",")

        results: Dict[str, Dict] = {}
        for code, meta in self.INDEX_SERIES.items():
            fields = payloads.get(code)
            if not fields or len(fields) < 6:
                results[code] = {"key": code, "name": meta["name"], "error": "行情为空"}
                continue
            try:
                price = float(fields[3]) if fields[3] else None
                prev = float(fields[2]) if fields[2] else None
                pct = None
                if price is not None and prev not in (None, 0):
                    pct = (price - prev) / prev * 100
                results[code] = {
                    "key": code,
                    "name": meta["name"],
                    "value": price,
                    "unit": "",
                    "change_pct": pct,
                    "source": meta.get("source"),
                    "updated_at": f"{fields[30] if len(fields) > 30 else ''} {fields[31] if len(fields) > 31 else ''}".strip(),
                }
            except (ValueError, IndexError):
                results[code] = {
                    "key": code,
                    "name": meta["name"],
                    "error": "解析失败",
                }
        return results

    def _fetch_sep_real_rate(self) -> Optional[Dict]:
        sep_date = self._find_latest_sep_date()
        if not sep_date:
            return {
                "key": "SEP_REAL_RATE",
                "name": "政策实际利率(SEP年末- PCE)",
                "error": "未找到SEP数据",
                "source": "FOMC SEP",
            }
        url = f"https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{sep_date}.htm"
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            Logger.error(f"fetch SEP table failed: {exc}")
            return {
                "key": "SEP_REAL_RATE",
                "name": "政策实际利率(SEP年末- PCE)",
                "error": "SEP表格获取失败",
                "source": "FOMC SEP",
            }
        fed_row = self._extract_sep_row(resp.text, "Federal funds rate")
        pce_row = self._extract_sep_row(resp.text, "PCE inflation")
        if not fed_row or not pce_row:
            return {
                "key": "SEP_REAL_RATE",
                "name": "政策实际利率(SEP年末- PCE)",
                "error": "SEP表格解析失败",
                "source": "FOMC SEP",
            }
        fed_median = self._extract_first_number(fed_row[1:])
        pce_median = self._extract_first_number(pce_row[1:])
        if fed_median is None or pce_median is None:
            return {
                "key": "SEP_REAL_RATE",
                "name": "政策实际利率(SEP年末- PCE)",
                "error": "SEP数值缺失",
                "source": "FOMC SEP",
            }
        return {
            "key": "SEP_REAL_RATE",
            "name": "政策实际利率(SEP年末- PCE)",
            "value": fed_median - pce_median,
            "unit": "%",
            "updated_at": sep_date,
            "source": "FOMC SEP",
            "components": {
                "fed_funds_median": fed_median,
                "pce_median": pce_median,
            },
        }

    def _find_latest_sep_date(self) -> Optional[str]:
        try:
            resp = self.session.get(self.SEP_CALENDAR_URL, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            Logger.error(f"fetch SEP calendar failed: {exc}")
            return None
        matches = self.SEP_TABLE_PATTERN.findall(resp.text)
        if not matches:
            return None
        return max(matches)

    def _extract_sep_row(self, html: str, label: str) -> Optional[List[str]]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
            if not cells:
                continue
            if cells[0].lower().startswith(label.lower()):
                return cells
        return None

    def _extract_first_number(self, cells: List[str]) -> Optional[float]:
        for cell in cells:
            raw = cell.replace(",", "").strip()
            if re.fullmatch(r"-?\d+(\.\d+)?", raw):
                return float(raw)
        for cell in cells:
            raw = cell.replace(",", "").strip()
            match = re.search(r"-?\d+(?:\.\d+)?", raw)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    continue
        return None

    def _fetch_bloomberg_credit_impulse(self) -> Optional[Dict]:
        url = "https://r.jina.ai/http://sc.macromicro.me/charts/data/35559.csv"
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            payload = resp.text.strip()
            if payload.startswith("Title:"):
                payload = payload.split("Markdown Content:", 1)[-1].strip()
            data = json.loads(payload)
        except Exception as exc:
            Logger.error(f"fetch credit impulse failed: {exc}")
            return {
                "key": "BB_CREDIT_IMPULSE_CN",
                "name": "中国-彭博信贷脉冲指数",
                "error": "数据获取失败",
                "source": "MacroMicro",
            }
        if not isinstance(data, dict) or not data.get("success"):
            msg = data.get("msg") if isinstance(data, dict) else None
            return {
                "key": "BB_CREDIT_IMPULSE_CN",
                "name": "中国-彭博信贷脉冲指数",
                "error": msg or "需要会员授权",
                "source": "MacroMicro",
            }
        series = data.get("data") or []
        if not series:
            return {
                "key": "BB_CREDIT_IMPULSE_CN",
                "name": "中国-彭博信贷脉冲指数",
                "error": "数据为空",
                "source": "MacroMicro",
            }
        last_point = series[-1]
        try:
            date_str = str(last_point[0])
            value = float(last_point[1])
        except Exception:
            return {
                "key": "BB_CREDIT_IMPULSE_CN",
                "name": "中国-彭博信贷脉冲指数",
                "error": "数据解析失败",
                "source": "MacroMicro",
            }
        return {
            "key": "BB_CREDIT_IMPULSE_CN",
            "name": "中国-彭博信贷脉冲指数",
            "value": value,
            "unit": "",
            "updated_at": date_str,
            "source": "MacroMicro",
        }
