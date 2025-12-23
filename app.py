from flask import Flask, render_template, request, redirect, url_for
import json
import math
import numpy as np
import pandas as pd
from pathlib import Path
import datetime
from datetime import timedelta
import random
import functools
import os
import time
from typing import List
from flask import make_response, jsonify, Response
from data import get_exchange_filter
from backtest.screener import Screener
from backtest.data_manager import change_ts
from logger import Logger
import traceback
import requests
from services.utils import (
    date2int,
    format_beijing_timestamp,
    beijing_now,
    convert_numpy_types,
)
from services.name_cache import StockNameCache
from services.backtest_service import BacktestService
from services.strategy_store import StrategyStore
from services.monitor_service import FuturesMonitorService
from services.basis_history import BasisHistory
from services.vix_history import VixHistoryStore
from services.afterhours_service import AfterHoursService
from services.macro_service import MacroDataService
from services.fedwatch_service import FedWatchService

import tushare as ts
try:
    from data_factory.setting import token as TS_TOKEN
except Exception:
    TS_TOKEN = None

exchange_name_dict = {
    "BSE": "北交所",
    "SZSE": "深交所",
    "SSE": "上交所"
}

token = TS_TOKEN or "1e266a5110f1d8fd926d3af0d034458b9d5c904636c72c723ab9fa38"
pro = ts.pro_api(token)
stock_name_cache = StockNameCache(pro, cache_file=Path(__file__).resolve().parent / "data" / "stock_names.json")
stock_name_cache.ensure_cache()
backtest_service = BacktestService('strategy_config.json.template', stock_name_cache)
strategy_store = StrategyStore(Path(__file__).resolve().parent / "data" / "saved_strategies.json")
basis_history = BasisHistory(pro, Path(__file__).resolve().parent / "data" / "futures_basis_history.json")
basis_history.ensure_latest()
monitor_service = FuturesMonitorService(basis_history=basis_history, pro_client=pro)
vix_history_store = VixHistoryStore(Path(__file__).resolve().parent / "data" / "vix_history.json")
after_hours_service = AfterHoursService(pro, Path(__file__).resolve().parent / "data" / "after_hours_history.json")
macro_service = MacroDataService()
fedwatch_service = FedWatchService(
    cache_file=Path(__file__).resolve().parent / "data" / "fedwatch_cache.json",
    calendar_file=Path(__file__).resolve().parent / "data" / "fomc_calendar.json",
    history_file=Path(__file__).resolve().parent / "data" / "fedwatch_history.json",
)

DEBUG_MODE = os.getenv("JC_DEBUG", "0") == "1"
CALENDAR_CSV_PATH = Path("/home/zbx/python_utils/python_utils/calendar.csv")
CALENDAR_DATE_CACHE = None


def debug_print(*args, **kwargs):
    if DEBUG_MODE:
        print(*args, **kwargs)


def load_valid_trade_dates() -> List[str]:
    global CALENDAR_DATE_CACHE
    if CALENDAR_DATE_CACHE is not None:
        return CALENDAR_DATE_CACHE
    dates: List[str] = []
    try:
        lines = CALENDAR_CSV_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        CALENDAR_DATE_CACHE = []
        return CALENDAR_DATE_CACHE
    for line in lines:
        raw = line.strip()
        if len(raw) != 8 or not raw.isdigit():
            continue
        dates.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    CALENDAR_DATE_CACHE = sorted(set(dates))
    return CALENDAR_DATE_CACHE


def resolve_valid_date(date_str: str, valid_set: set) -> str:
    if date_str in valid_set:
        return date_str
    try:
        cursor = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return date_str
    for _ in range(370):
        cursor -= datetime.timedelta(days=1)
        candidate = cursor.strftime("%Y-%m-%d")
        if candidate in valid_set:
            return candidate
    return date_str

FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cnn.com/markets/fear-and-greed"
}
CN_HISTORY_CSV = Path(__file__).resolve().parent / "data" / "cn_sentiment_history.csv"
MANDATORY_SENTIMENT_DATES = {"20251210", "20251216"}

app = Flask(__name__)

# 当前日期作为默认筛选日期
TODAY = datetime.date.today().isoformat()
screener = Screener('strategy_config.json.template')
TRADE_DAY_CACHE = {}


def resolve_trade_date_for_cutoff(cutoff_hour: int = 15) -> str:
    """Return latest trade date string based on cutoff hour (Beijing time)."""
    now = beijing_now()
    base_date = now.date() if now.hour >= cutoff_hour else now.date() - datetime.timedelta(days=1)
    return ensure_trade_date(base_date)


def ensure_trade_date(date_obj: datetime.date) -> str:
    for offset in range(10):
        candidate = date_obj - datetime.timedelta(days=offset)
        date_str = candidate.strftime("%Y%m%d")
        if is_trade_day(date_str):
            return date_str
    return date_obj.strftime("%Y%m%d")


def is_trade_day(date_str: str) -> bool:
    if date_str in TRADE_DAY_CACHE:
        return TRADE_DAY_CACHE[date_str]
    try:
        cal = pro.trade_cal(exchange="SSE", start_date=date_str, end_date=date_str)
        is_open = bool(cal is not None and not cal.empty and str(cal.iloc[0]["is_open"]) == "1")
    except Exception:
        is_open = False
    TRADE_DAY_CACHE[date_str] = is_open
    return is_open


def no_cache(view):
    @functools.wraps(view)
    def decorated_view(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    return decorated_view

@app.errorhandler(Exception)
def handle_global_exception(e):
    """记录错误和堆栈"""
    Logger.error(f"Uncaught exception occurred:{str(e)}")
    Logger.error("traceback:\n" + traceback.format_exc())
    # 确保返回有效的响应，避免 Flask 抛出 TypeError
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({"error": "internal server error", "message": str(e)}), 500
    return make_response(f"Internal Server Error: {e}", 500)

def fetch_fear_greed():
    """拉取CNN恐惧与贪婪指数数据"""
    resp = requests.get(FEAR_GREED_URL, headers=FEAR_GREED_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()

def parse_fear_greed_payload(payload):
    """整理主指标和子指标数据，便于模板展示"""
    main = payload.get('fear_and_greed', {}) or {}
    history_points = payload.get('fear_and_greed_historical', {}).get('data', []) or []

    # 若实时主指标缺失，尝试用历史最后一个点兜底
    if not main.get('score') and history_points:
        last_point = history_points[-1]
        main['score'] = last_point.get('y')
        main['rating'] = last_point.get('rating', main.get('rating'))
        main['timestamp'] = last_point.get('x')

    main['timestamp_cn'] = format_beijing_timestamp(main.get('timestamp'))

    indicator_name_map = {
        'market_momentum_sp500': '市场动量(S&P 500)',
        'market_momentum_sp125': '市场动量(S&P 125日均线)',
        'stock_price_strength': '价格强度(52周新高/低)',
        'stock_price_breadth': '价格广度(涨跌比)',
        'put_call_options': '期权看跌看涨比',
        'market_volatility_vix': '波动率(VIX)',
        'market_volatility_vix_50': '波动率(VIX 50日均线)',
        'junk_bond_demand': '高收益债需求',
        'safe_haven_demand': '避险需求'
    }

    sub_list = []
    for key, label in indicator_name_map.items():
        item = payload.get(key)
        if not isinstance(item, dict):
            continue
        sub_list.append({
            'key': key,
            'label': label,
            'score': item.get('score'),
            'rating': item.get('rating'),
            'timestamp': item.get('timestamp'),
            'timestamp_cn': format_beijing_timestamp(item.get('timestamp')),
            'chart': item.get('data', [])[:60],  # 近期数据，防止过长
        })

    extras = {
        "momentum_series": payload.get("market_momentum_sp500", {}).get("data", []),
        "momentum_ma": payload.get("market_momentum_sp125", {}).get("data", []),
        "vix_series": payload.get("market_volatility_vix", {}).get("data", []),
        "vix_ma": payload.get("market_volatility_vix_50", {}).get("data", []),
    }

    return main, history_points, sub_list, extras

def score_rating(score):
    if score is None:
        return "n/a"
    if score < 25:
        return "extreme fear"
    if score < 45:
        return "fear"
    if score < 55:
        return "neutral"
    if score < 75:
        return "greed"
    return "extreme greed"

def scale_0_100(value, low, high):
    if value is None:
        return 50
    if high == low:
        return 50
    clipped = max(min(value, high), low)
    return (clipped - low) / (high - low) * 100

def compute_cn_sentiment(trade_date=None):
    """基于A股行情，按CNN Fear & Greed思路的简化版：
    - 市场动量：沪深300收盘/125日均线
    - 价格广度：上涨家数占比
    - 价格强度：涨停-跌停占比差
    - 波动率：沪深300近20日年化波动（反向处理）
    - 平均涨跌：全市场日均涨跌幅
    默认：17:00前使用前一交易日，17:00后使用当日。
    """
    if trade_date is None:
        now_bj = beijing_now()
        target_date = now_bj.date() if now_bj.hour >= 17 else now_bj.date() - datetime.timedelta(days=1)
        trade_date = target_date.strftime("%Y%m%d")
    elif "-" in str(trade_date):
        trade_date = trade_date.replace("-", "")

    df = pro.daily(trade_date=trade_date)
    if df is None or df.empty:
        return None

    total = len(df)
    if total == 0:
        return None

    pct = df['pct_chg']
    adv_ratio = (pct > 0).sum() / total
    limit_up = (pct >= 9.5).sum() / total
    limit_down = (pct <= -9.5).sum() / total
    avg_pct = float(pct.mean())

    # 指数动量 & 波动率（沪深300）
    start_hist = (datetime.datetime.strptime(trade_date, "%Y%m%d") - datetime.timedelta(days=220)).strftime("%Y%m%d")
    idx_df = pro.index_daily(ts_code="000300.SH", start_date=start_hist, end_date=trade_date)
    idx_df = idx_df.sort_values("trade_date")
    if idx_df.empty:
        return None
    idx_df["ret"] = idx_df["close"].pct_change()
    idx_df["ma125"] = idx_df["close"].rolling(125, min_periods=20).mean()
    last_idx = idx_df.iloc[-1]
    momentum_val = (last_idx["close"] / last_idx["ma125"] - 1) if last_idx["ma125"] else 0
    vol20 = idx_df["ret"].tail(20).std() * np.sqrt(252)

    # 看涨看跌比（沪深300期权 IO，按成交量）
    pcr_val = None
    try:
        opt_df = pro.opt_daily(trade_date=trade_date, fields="ts_code,trade_date,call_put,vol")
        if opt_df is not None and not opt_df.empty:
            io_df = opt_df[opt_df["ts_code"].str.startswith("IO")]
            if not io_df.empty:
                call_vol = io_df[io_df["call_put"] == "C"]["vol"].sum()
                put_vol = io_df[io_df["call_put"] == "P"]["vol"].sum()
                if call_vol and put_vol:
                    pcr_val = put_vol / call_vol
    except Exception as e:
        Logger.error(f"compute pcr failed: {e}")

    sub_indicators = [
        {"label": "市场动量(沪深300/125日均)", "value": momentum_val, "score": scale_0_100(momentum_val, -0.05, 0.05)},
        {"label": "价格广度(上涨家数占比)", "value": adv_ratio, "score": scale_0_100(adv_ratio, 0.2, 0.8)},
        {"label": "价格强度(涨停-跌停占比)", "value": limit_up - limit_down, "score": scale_0_100(limit_up - limit_down, -0.05, 0.05)},
        {"label": "波动率(反向)", "value": vol20, "score": 100 - scale_0_100(vol20, 0.08, 0.35)},
        {"label": "平均涨跌幅", "value": avg_pct, "score": scale_0_100(avg_pct, -2.0, 2.0)},
    ]
    if pcr_val is not None:
        sub_indicators.append({
            "label": "沪深300期权PCR(IO)",
            "value": pcr_val,
            "score": 100 - scale_0_100(pcr_val, 0.7, 1.3)
        })
    for item in sub_indicators:
        item["rating"] = score_rating(item["score"])

    main_score = float(np.mean([s["score"] for s in sub_indicators]))
    rating = score_rating(main_score)
    history_points = [{"x": int(datetime.datetime.strptime(trade_date, "%Y%m%d").timestamp() * 1000), "y": main_score, "rating": rating}]

    return {
        "trade_date": trade_date,
        "main": {
            "score": round(main_score, 2),
            "rating": rating,
            "timestamp": trade_date,
            "timestamp_cn": format_beijing_timestamp(trade_date),
            "previous_close": None,
            "previous_1_week": None,
            "previous_1_month": None,
            "previous_1_year": None,
        },
        "history": history_points,
        "subs": sub_indicators
    }

def persist_cn_sentiment(trade_date, main, subs):
    """将当日情绪指标追加到CSV"""
    CN_HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "trade_date": trade_date,
        "score": main.get("score"),
        "rating": main.get("rating"),
    }
    for item in subs:
        key = item["label"]
        # 简化列名
        col = {
            "市场动量(沪深300/125日均)": "momentum",
            "价格广度(上涨家数占比)": "breadth",
            "价格强度(涨停-跌停占比)": "strength",
            "波动率(反向)": "volatility",
            "平均涨跌幅": "avgret",
            "沪深300期权PCR(IO)": "pcr",
        }.get(key, key)
        row[f"{col}_val"] = item.get("value")
        row[f"{col}_score"] = item.get("score")
    df_new = pd.DataFrame([row])
    if CN_HISTORY_CSV.exists():
        # 若已存在当日记录则不重复写入
        df_old = pd.read_csv(CN_HISTORY_CSV)
        if str(trade_date) in df_old["trade_date"].astype(str).values:
            return
    df_new.to_csv(CN_HISTORY_CSV, mode='a', index=False, header=not CN_HISTORY_CSV.exists())

def backfill_cn_sentiment_upto(target_date: str, lookback_days: int = 420):
    if not target_date:
        return
    if "-" in str(target_date):
        target_date = target_date.replace("-", "")
    df = pd.read_csv(CN_HISTORY_CSV) if CN_HISTORY_CSV.exists() else pd.DataFrame()
    existing_dates = set(df["trade_date"].astype(str)) if not df.empty else set()

    if not existing_dates:
        candidates = [target_date]
    else:
        try:
            target_dt = datetime.datetime.strptime(target_date, "%Y%m%d").date()
        except ValueError:
            return
        lookback_dt = target_dt - datetime.timedelta(days=lookback_days)
        start_date = min(existing_dates)
        if start_date:
            try:
                start_dt = datetime.datetime.strptime(start_date, "%Y%m%d").date()
            except ValueError:
                start_dt = lookback_dt
        else:
            start_dt = lookback_dt
        start_dt = max(start_dt, lookback_dt)
        try:
            cal = pro.trade_cal(
                exchange="SSE",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=target_date,
                is_open="1",
            )
        except Exception as exc:
            Logger.error(f"补全情绪交易日历失败: {exc}")
            return
        candidates = cal["cal_date"].astype(str).tolist() if cal is not None and not cal.empty else []

    for d in sorted(set(candidates) - existing_dates):
        try:
            res = compute_cn_sentiment(d)
            if res:
                persist_cn_sentiment(res["trade_date"], res["main"], res["subs"])
                Logger.info(f"补全缺失情绪数据: {d}")
        except Exception as exc:
            Logger.error(f"补全情绪数据失败: {d}, err={exc}")


def load_cn_history(required_dates=None):
    """加载历史数据，必要时补齐必需日期"""
    required = set(MANDATORY_SENTIMENT_DATES)
    if required_dates:
        required.update(str(d).replace("-", "") for d in required_dates)

    df = pd.read_csv(CN_HISTORY_CSV) if CN_HISTORY_CSV.exists() else pd.DataFrame()
    existing_dates = set(df["trade_date"].astype(str)) if not df.empty else set()

    missing = sorted(required - existing_dates)
    if missing:
        for d in missing:
            try:
                res = compute_cn_sentiment(d)
                if res:
                    persist_cn_sentiment(res["trade_date"], res["main"], res["subs"])
                    Logger.info(f"补全缺失情绪数据: {d}")
            except Exception as exc:
                Logger.error(f"补全情绪数据失败: {d}, err={exc}")
        df = pd.read_csv(CN_HISTORY_CSV) if CN_HISTORY_CSV.exists() else pd.DataFrame()

    if df.empty:
        return []

    df = df.sort_values("trade_date")
    return df.to_dict(orient="records")

def build_subs_from_record(rec):
    """从历史记录重建细分指标列表"""
    mapping = [
        ("市场动量(沪深300/125日均)", "momentum_val", "momentum_score"),
        ("价格广度(上涨家数占比)", "breadth_val", "breadth_score"),
        ("价格强度(涨停-跌停占比)", "strength_val", "strength_score"),
        ("波动率(反向)", "volatility_val", "volatility_score"),
        ("平均涨跌幅", "avgret_val", "avgret_score"),
        ("沪深300期权PCR(IO)", "pcr_val", "pcr_score"),
    ]
    subs = []
    for label, val_key, score_key in mapping:
        val = rec.get(val_key)
        score_val = rec.get(score_key)
        subs.append({
            "label": label,
            "value": val,
            "score": score_val,
            "rating": score_rating(score_val) if score_val is not None else None
        })
    # 若缺失PCR字段，从记录中追加
    if not any(s["label"].startswith("沪深300期权PCR") for s in subs):
        if "pcr_val" in rec or "pcr_score" in rec:
            subs.append({
                "label": "沪深300期权PCR(IO)",
                "value": rec.get("pcr_val"),
                "score": rec.get("pcr_score"),
                "rating": score_rating(rec.get("pcr_score")) if rec.get("pcr_score") is not None else None
            })
    return subs
@app.template_filter('format_date')
def format_date(date_obj, fmt='%Y-%m-%d'):
    """日期格式化过滤器"""
    if isinstance(date_obj, str):
        try:
            date_obj = datetime.datetime.strptime(date_obj, '%Y-%m-%d')
        except ValueError:
            return date_obj
    return date_obj.strftime(fmt)

@app.route('/')
# @no_cache
def index():
    # 渲染筛选条件表单页
    valid_dates = load_valid_trade_dates()
    valid_set = set(valid_dates)
    now = beijing_now()
    base_date = now.date() if now.hour >= 18 else now.date() - datetime.timedelta(days=1)
    default_date = base_date.isoformat()
    if valid_set:
        default_date = resolve_valid_date(default_date, valid_set)
        today_display = valid_dates[-1]
    else:
        today_display = TODAY
    return render_template('index.html', 
                           today=today_display,
                           default_date=default_date,
                           valid_dates_json=json.dumps(valid_dates, ensure_ascii=False))


@app.route('/cb-screener')
def cb_screener():
    valid_dates = load_valid_trade_dates()
    valid_set = set(valid_dates)
    now = beijing_now()
    base_date = now.date() if now.hour >= 18 else now.date() - datetime.timedelta(days=1)
    default_date = base_date.isoformat()
    if valid_set:
        default_date = resolve_valid_date(default_date, valid_set)
        today_display = valid_dates[-1]
    else:
        today_display = TODAY
    return render_template(
        'cb_index.html',
        today=today_display,
        default_date=default_date,
        valid_dates_json=json.dumps(valid_dates, ensure_ascii=False),
    )

@app.route('/fear-greed')
def fear_greed():
    try:
        payload = fetch_fear_greed()
        main, history_points, sub_list, extras = parse_fear_greed_payload(payload)
    except Exception as e:
        Logger.error(f"fetch fear & greed failed: {e}")
        main, history_points, sub_list, extras = {}, [], [], {}
        error = "获取CNN Fear & Greed数据失败，请稍后再试。"
    else:
        error = None

    return render_template(
        'fear_greed.html',
        main=main,
        history=history_points[:120],
        sub_indicators=sub_list,
        extras=extras,
        error=error
    )

@app.route('/monitor')
def monitor_page():
    return render_template('monitor.html')

@app.route('/monitor/data')
def monitor_data():
    try:
        payload = monitor_service.get_monitor_payload()
        trade_date = resolve_trade_date_for_cutoff(15)
        vix_history_store.record(trade_date, payload.get("vix", []))
        payload["vix_history"] = vix_history_store.get_history()
        payload["macro"] = macro_service.get_macro_snapshot()
        return jsonify(convert_numpy_types(payload))
    except Exception as exc:
        Logger.error(f"fetch monitor data failed: {exc}")
        return jsonify({"error": "获取监控数据失败"}), 500

@app.route('/after-hours')
def after_hours_page():
    return render_template('after_hours.html')

@app.route('/after-hours/data')
def after_hours_data():
    try:
        payload = after_hours_service.get_payload()
        payload["fedwatch"] = fedwatch_service.get_snapshot()
        return jsonify(convert_numpy_types(payload))
    except Exception as exc:
        Logger.error(f"fetch after-hours data failed: {exc}")
        return jsonify({"error": "获取盘后监控数据失败"}), 500

@app.route('/cn-fear')
def cn_fear():
    trade_date = request.args.get("trade_date")
    if trade_date and "-" in trade_date:
        trade_date = trade_date.replace("-", "")
    if trade_date:
        backfill_target = trade_date
    else:
        now_bj = beijing_now()
        base_date = now_bj.date() if now_bj.hour >= 17 else now_bj.date() - datetime.timedelta(days=1)
        backfill_target = ensure_trade_date(base_date)
    backfill_cn_sentiment_upto(backfill_target)
    try:
        page = int(request.args.get("page", 1))
        page = max(page, 1)
    except (TypeError, ValueError):
        page = 1
    score_min_raw = request.args.get("score_min")
    score_max_raw = request.args.get("score_max")

    def parse_score(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    score_min_val = parse_score(score_min_raw)
    score_max_val = parse_score(score_max_raw)
    per_page = 20

    try:
        result = compute_cn_sentiment(trade_date)
        if result:
            persist_cn_sentiment(result["trade_date"], result["main"], result["subs"])
    except Exception as e:
        Logger.error(f"compute cn fear failed: {e}")
        result = None

    history_records = load_cn_history()

    if not result and not history_records:
        error = "获取当日行情或计算情绪失败，请稍后重试。"
        main = {}
        history = []
        subs = []
    else:
        error = None if result else "当日计算失败，显示历史数据。"
        if result:
            main = result["main"]
            subs = result["subs"]
        else:
            last = history_records[-1]
            main = {
                "score": last.get("score"),
                "rating": last.get("rating"),
                "timestamp": last.get("trade_date"),
                "timestamp_cn": format_beijing_timestamp(str(last.get("trade_date"))),
            }
            subs = build_subs_from_record(last)
        history = [
            {
                "x": int(datetime.datetime.strptime(str(rec["trade_date"]), "%Y%m%d").timestamp() * 1000),
                "y": rec.get("score"),
                "rating": rec.get("rating"),
            }
            for rec in history_records
        ]

    history_desc = list(reversed(history_records))

    def match_score(row):
        score = row.get("score")
        if score_min_val is not None and (score is None or float(score) < score_min_val):
            return False
        if score_max_val is not None and (score is None or float(score) > score_max_val):
            return False
        return True

    filtered_records = [rec for rec in history_desc if match_score(rec)]
    total_filtered = len(filtered_records)
    if total_filtered == 0:
        total_pages = 1
        page = 1
    else:
        total_pages = max(1, math.ceil(total_filtered / per_page))
        page = min(page, total_pages)
    start_idx = (page - 1) * per_page
    history_page = filtered_records[start_idx:start_idx + per_page]

    pagination = {
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "total": total_filtered
    }

    return render_template(
        'cn_fear.html',
        main=main,
        history=history,
        sub_indicators=subs,
        trade_date=trade_date or (result["trade_date"] if result else None),
        history_table=history_page,
        pagination=pagination,
        score_min_value=score_min_raw or "",
        score_max_value=score_max_raw or "",
        error=error
    )

def execute_screen_logic(filter_date, exclude_exchanges, exclude_st, filter_pe_gt_zero, sort_by):
    start_time = time.time()
    debug_print('screener start at ', start_time)

    screener.strategy_name_dict['trade_strategy']['end_time'] = date2int(TODAY)
    screener.strategy_name_dict['select_strategy']['st_strategy']['exclude'] = exclude_st

    mapping = {'main': 'zb_strategy', 'kcb': 'kcb_strategy', 'cyb': 'cyb_strategy', 'bse': 'bse_strategy'}
    for k, strategy_name in mapping.items():
        screener.strategy_name_dict['select_strategy'][strategy_name]['exclude'] = k in (exclude_exchanges or [])

    if not filter_pe_gt_zero:
        screener.strategy_name_dict['select_strategy'].pop('xsz_strategy', None)
    else:
        screener.strategy_name_dict['select_strategy']['xsz_strategy'] = screener.strategy_name_dict['select_strategy'].get('xsz_strategy', {})

    error_msg = None
    try:
        stocks = screener.select(date2int(filter_date)) or []
    except Exception as e:
        Logger.error(f"run_screener failed: {e}")
        Logger.error(traceback.format_exc())
        stocks = []
        error_msg = str(e)

    res = []
    if stocks:
        is_st_dg = screener.dm.is_st(screener.stock_list, date2int(filter_date))
        close_dg = screener.dm.close(screener.stock_list, date2int(filter_date), adj=False)
        total_mv = screener.dm.total_mv(screener.stock_list, date2int(filter_date)) * 1e4
        net_assets = screener.dm.net_assets(screener.stock_list, date2int(filter_date))
        goodwill = np.nan_to_num(screener.dm.goodwill(screener.stock_list, date2int(filter_date)))
        profit_dedtQ = screener.dm.profit_dedtQ(screener.stock_list, date2int(filter_date))
        ewm_amount = screener.dm.ewm_amount(screener.stock_list, date2int(filter_date))

        avg_total_mv = []
        for index, (code, score) in enumerate(stocks, start=1):
            idx = np.where(screener.stock_list == code)[0][0]
            data = pro.stock_basic(ts_code=change_ts(str(code)), fields='ts_code,symbol,name,exchange')
            avg_total_mv.append(total_mv[idx])
            res.append({
                'index': index,
                'score': score,
                'id': str(code),
                'name': data.name.values[0],
                'exchange_name': exchange_name_dict[data.exchange.values[0]],
                'is_st': bool(is_st_dg[idx]),
                'closing_price': close_dg[idx],
                'market_cap': total_mv[idx] / 1e8,
                'quarterly_pe': total_mv[idx] / profit_dedtQ[idx] / 4 if profit_dedtQ[idx] else None,
                'quarterly_net_profit': profit_dedtQ[idx] / 1e8,
                'goodwill': goodwill[idx] / 1e8,
                'net_assets': net_assets[idx] / 1e8,
                'adjusted_pb': total_mv[idx] / (net_assets[idx] - goodwill[idx]) if (net_assets[idx] - goodwill[idx]) else None,
                'ewm_amount': ewm_amount[-1, idx] / 10 if ewm_amount is not None else None
            })
    exchange_data = get_exchange_filter()
    debug_print('screener result len', len(res))
    return res, error_msg, exchange_data


@app.route('/screener', methods=['POST'])
def run_screener():
    filter_date = request.form['filter_date']
    exclude_exchanges = request.form.getlist('exclude_exchanges')
    exclude_st = 'exclude_st' in request.form
    filter_pe_gt_zero = 'filter_pe_gt_zero' in request.form
    sort_by = request.form.get('sort_by', 'market_cap_asc')

    res, error_msg, exchange_data = execute_screen_logic(
        filter_date, exclude_exchanges, exclude_st, filter_pe_gt_zero, sort_by
    )

    return render_template('result.html',
                           stocks=res[:20],
                           total_count=len(res),
                           error=error_msg,
                           filter_date=filter_date,
                           exclude_exchanges=exclude_exchanges,
                           exclude_st=exclude_st,
                           filter_pe_gt_zero=filter_pe_gt_zero,
                           sort_by=sort_by,
                           exchange_data=exchange_data)


@app.route('/cb-screener', methods=['POST'])
def run_cb_screener():
    filter_date = request.form['filter_date']
    hold_num = int(request.form.get('hold_num', 10))
    rebalance_period = int(request.form.get('rebalance_period', 1))
    rebalance_time = request.form.get('rebalance_time', 'close')
    exclude_exchanges = request.form.getlist('exclude_exchanges')
    exclude_st = 'exclude_st' in request.form
    filter_pe_gt_zero = 'filter_pe_gt_zero' in request.form

    filter_conditions = {
        "exclude_exchanges": exclude_exchanges,
        "exclude_st": exclude_st,
        "filter_pe_gt_zero": filter_pe_gt_zero,
    }
    backtest_params = {
        "start_date": filter_date,
        "end_date": filter_date,
        "hold_stocks": hold_num,
        "rebalance_period": rebalance_period,
        "rebalance_time": rebalance_time,
    }

    error_msg = None
    positions = []
    try:
        result = backtest_service.run(filter_conditions, backtest_params)
        history = result.get("position_history") if isinstance(result, dict) else []
        if history:
            latest = history[-1]
            positions = latest.get("positions", [])
    except Exception as exc:
        Logger.error(f"cb screener failed: {exc}")
        Logger.error(traceback.format_exc())
        error_msg = str(exc)

    return render_template(
        'cb_result.html',
        positions=positions,
        total_count=len(positions),
        filter_date=filter_date,
        hold_num=hold_num,
        rebalance_period=rebalance_period,
        rebalance_time=rebalance_time,
        exclude_exchanges=exclude_exchanges,
        exclude_st=exclude_st,
        filter_pe_gt_zero=filter_pe_gt_zero,
        error=error_msg,
    )


@app.route('/screener/export')
def export_screener():
    filter_date = request.args.get('filter_date', TODAY)
    exclude_exchanges = request.args.getlist('exclude_exchanges')
    exclude_st = 'exclude_st' in request.args
    filter_pe_gt_zero = 'filter_pe_gt_zero' in request.args
    sort_by = request.args.get('sort_by', 'market_cap_asc')

    res, _, _ = execute_screen_logic(filter_date, exclude_exchanges, exclude_st, filter_pe_gt_zero, sort_by)
    lines = ["代码,名字"] + [f"{item['id']},{item['name']}" for item in res]
    csv_data = "\n".join(lines)
    filename = f"screener_{filter_date}.csv"
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


@app.route('/backtest')
# @no_cache
def backtest_page():
    # 历史数据的最早日期 (假设有2年历史数据)
    min_history_date = (datetime.date.today() - timedelta(days=365)).isoformat()
    end_date = (datetime.date.today() - timedelta(days=1)).isoformat()
    
    return render_template('backtest.html', 
                           start_date=min_history_date,
                           end_date=end_date,
                           min_history_date=min_history_date[:10])

@app.route('/backtest', methods=['POST'])
def run_backtest():
    data = request.get_json() or {}
    filter_conditions = data.get('filter', {})
    backtest_params = data.get('params', {})
    try:
        result = backtest_service.run(filter_conditions, backtest_params)
        return jsonify(result)
    except Exception as ex:
        Logger.error(f"backtest failed: {ex}")
        Logger.error(traceback.format_exc())
        return jsonify({"error": "回测失败", "message": str(ex)}), 500


@app.route('/strategies', methods=['GET', 'POST'])
def strategies():
    if request.method == 'GET':
        return jsonify(strategy_store.list_strategies())
    data = request.get_json() or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "策略名称不能为空"}), 400
    payload = {
        "filter": data.get("filter"),
        "params": data.get("params")
    }
    strategy_store.save(name, payload)
    return jsonify({"status": "ok"})


@app.route('/strategies/<name>', methods=['GET'])
def get_strategy(name):
    item = strategy_store.get(name)
    if not item:
        return jsonify({"error": "策略不存在"}), 404
    return jsonify(item)


@app.route('/portfolio')
def portfolio_page():
    return render_template('portfolio.html')


def generate_backtest_results(params):
    # 模拟回测结果数据
    start_date = datetime.datetime.strptime(params['start_date'], '%Y-%m-%d') - timedelta(days=20)
    end_date =   datetime.datetime.strptime(params['end_date'], '%Y-%m-%d')
    days = (end_date - start_date).days
    
    # 生成时间序列标签
    dates = []
    for i in range(days):
        dates.append((start_date + timedelta(days=i)).strftime('%Y-%m-%d'))
    
    # 生成策略和基准收益序列
    strategy, benchmark = [1.0], [1.0]
    for i in range(1, days):
        # 模拟每日波动
        strategy_ret = 0.0008 + random.normalvariate(0, 0.015)
        bench_ret = 0.0004 + random.normalvariate(0, 0.008)
        
        # 添加到累计净值
        strategy.append(strategy[-1] * (1 + strategy_ret))
        benchmark.append(benchmark[-1] * (1 + bench_ret))
    
    # 计算回撤序列
    drawdown = []
    peak = strategy[0]
    for value in strategy:
        if value > peak:
            peak = value
        drawdown.append((value - peak) / peak)
    
    # 查找最大回撤
    max_drawdown = min(drawdown)
    max_drawdown_index = drawdown.index(max_drawdown)
    # print(dates, max_drawdown_index)
    max_drawdown_period = f"{dates[0]}至{dates[-1]}"
    
    # 计算年化收益率
    total_return = strategy[-1] - 1
    annual_return = (1 + total_return) ** (365 / days) - 1
    
    # 计算夏普比率
    # 简化计算
    sharpe_ratio = (annual_return - 0.03) / (0.15 * total_return) + random.normalvariate(0, 0.2)
    
    # 超额收益
    excess_return = 1.0
    
    return {
        'annual_return': 0.1,
        'sharpe_ratio': 1.1,
        'max_drawdown': 0.1,
        'excess_return': 0.1,
        'benchmark': '沪深300',
        'max_drawdown_period': max_drawdown_period,
        'chart_data': {
            'labels': dates[::5],  # 每5天显示一个点
            'strategy': [round(x, 4) for x in strategy][::5],
            'benchmark': [round(x, 4) for x in benchmark][::5]
        },
        'drawdown_data': {
            'labels': dates[::7],
            'values': [round(x, 4) for x in drawdown][::7]
        }
    }

if __name__ == '__main__':
    app.run('0.0.0.0', port=5000)
@app.route('/portfolio/backtest', methods=['POST'])
def portfolio_backtest():
    data = request.get_json() or {}
    names = data.get('strategies') or []
    if len(names) < 2:
        return jsonify({"error": "至少选择两个策略"}), 400
    if len(names) > 3:
        return jsonify({"error": "最多同时评估3个策略"}), 400
    weight_step = float(data.get('weight_step', 0.25))
    if weight_step <= 0 or weight_step > 0.5:
        return jsonify({"error": "权重步长需在0.1-0.5之间"}), 400

    strategy_defs = []
    for name in names:
        item = strategy_store.get(name)
        if not item:
            return jsonify({"error": f"策略 {name} 不存在"}), 404
        strategy_defs.append(item)

    strategy_runs = []
    for item in strategy_defs:
        result = backtest_service.run(item['filter'], item['params'])
        nav_series = dict(zip(result['chart_data']['labels'], result['chart_data']['strategy']))
        strategy_runs.append({
            "name": item['name'],
            "labels": result['chart_data']['labels'],
            "nav": nav_series
        })

    common_labels = sorted(set.intersection(*[set(run['labels']) for run in strategy_runs]))
    if not common_labels:
        return jsonify({"error": "所选策略时间区间不一致，没有可比较数据"}), 400

    def enumerate_weights(n, step):
        values = [round(i * step, 4) for i in range(int(1/step)+1)]
        combos = []
        def helper(idx, remaining, current):
            if idx == n - 1:
                current.append(round(remaining, 4))
                if current[-1] >= 0:
                    combos.append(current.copy())
                current.pop()
                return
            for v in values:
                if v <= remaining:
                    current.append(v)
                    helper(idx + 1, round(remaining - v, 4), current)
                    current.pop()
        helper(0, 1.0, [])
        return [combo for combo in combos if all(w >= 0) and abs(sum(combo) - 1) < 1e-3]

    combos = enumerate_weights(len(strategy_runs), weight_step)
    if not combos:
        return jsonify({"error": "无法根据当前步长生成权重组合"}), 400

    portfolios = []
    dates = [datetime.datetime.strptime(lbl, "%Y-%m-%d") for lbl in common_labels]
    days = (dates[-1] - dates[0]).days or 1
    for combo in combos:
        nav = []
        for lbl in common_labels:
            value = 0
            for weight, run in zip(combo, strategy_runs):
                value += weight * run['nav'][lbl]
            nav.append(value)
        daily_returns = [nav[i] / nav[i-1] - 1 for i in range(1, len(nav))]
        total_return = nav[-1] / nav[0] - 1
        annual_return = (nav[-1] / nav[0]) ** (365 / days) - 1
        sharpe = 0
        if daily_returns:
            risk = np.std(daily_returns) * np.sqrt(252)
            if risk != 0:
                sharpe = (nav[-1] / nav[0] - 1) / risk
        portfolios.append({
            "label": " + ".join(f"{name}:{int(w*100)}%" for name, w in zip(names, combo)),
            "weights": combo,
            "total_return": total_return,
            "annual_return": annual_return,
            "sharpe_ratio": sharpe
        })

    best_return = max(portfolios, key=lambda x: x['annual_return'], default=None)
    best_sharpe = max(portfolios, key=lambda x: x['sharpe_ratio'], default=None)
    return jsonify({
        "strategy_names": names,
        "portfolios": portfolios,
        "best_return": best_return,
        "best_sharpe": best_sharpe
    })
