from flask import Flask, render_template, request, redirect, url_for
import json
import numpy as np
import pandas as pd
from pathlib import Path
import datetime
from datetime import timedelta
import random
import functools 
from flask import make_response, jsonify
from data import get_exchange_filter
from backtest.screener import Screener
from backtest.backtester import Backtester
from backtest.data_manager import change_ts
from logger import Logger
import traceback
import requests

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

FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cnn.com/markets/fear-and-greed"
}
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8), name="CST")
CN_HISTORY_CSV = Path(__file__).resolve().parent / "data" / "cn_sentiment_history.csv"

app = Flask(__name__)

# 当前日期作为默认筛选日期
TODAY = datetime.date.today().isoformat()
screener = Screener('strategy_config.json.template')


def no_cache(view):
    @functools.wraps(view)
    def decorated_view(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    return decorated_view

def date2int(date_str):
    """将日期字符串转换为整数格式"""
    return int(date_str.replace('-', ''))

@app.errorhandler(Exception)
def handle_global_exception(e):
    """记录错误和堆栈"""
    Logger.error(f"Uncaught exception occurred:{str(e)}")
    Logger.error("traceback:\n" + traceback.format_exc())

def format_beijing_timestamp(ts):
    """将时间戳/ISO字符串转为北京时间字符串"""
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            # CNN部分字段是毫秒时间戳
            if ts > 1e12:
                ts = ts / 1000.0
            dt_obj = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        elif isinstance(ts, str):
            if ts.isdigit() and len(ts) == 8:
                dt_obj = datetime.datetime.strptime(ts, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
            else:
                dt_obj = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            return None
        return dt_obj.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
    except Exception as ex:
        Logger.error(f"timestamp format failed: {ex}")
        return None

def beijing_now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(BEIJING_TZ)

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

def load_cn_history():
    if not CN_HISTORY_CSV.exists():
        return []
    df = pd.read_csv(CN_HISTORY_CSV)
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
    return render_template('index.html', 
                           today=TODAY,
                           default_date=(datetime.date.today()).isoformat())

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

@app.route('/cn-fear')
def cn_fear():
    trade_date = request.args.get("trade_date")
    if trade_date and "-" in trade_date:
        trade_date = trade_date.replace("-", "")
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

    return render_template(
        'cn_fear.html',
        main=main,
        history=history,
        sub_indicators=subs,
        trade_date=trade_date or (result["trade_date"] if result else None),
        history_table=history_records[::-1],  # 逆序便于展示最近的
        error=error
    )

@app.route('/screener', methods=['POST'])
def run_screener():
    # 获取用户设置的条件
    import time

    c1 = time.time()
    print('c1 = ', c1)
    filter_date = request.form['filter_date']
    exclude_exchanges = request.form.getlist('exclude_exchanges')
    exclude_st = 'exclude_st' in request.form
    filter_pe_gt_zero = 'filter_pe_gt_zero' in request.form
    sort_by = request.form.get('sort_by', 'market_cap_asc')
    print('exclude_exchanges ', exclude_exchanges)

    screener.strategy_name_dict['trade_strategy']['end_time'] = date2int(TODAY)
    screener.strategy_name_dict['select_strategy']['st_strategy']['exclude'] = exclude_st

    mapping = {
                'main': 'zb_strategy',
                'kcb': 'kcb_strategy',
                'cyb': 'cyb_strategy',
                'bse': 'bse_strategy'
    }
    for k in ['main', 'kcb', 'cyb', 'bse']:
        if k in exclude_exchanges:
            screener.strategy_name_dict['select_strategy'][mapping[k]]['exclude'] = True
        else:
            screener.strategy_name_dict['select_strategy'][mapping[k]]['exclude'] = False

    if not filter_pe_gt_zero:
        del screener.strategy_name_dict['select_strategy']['xsz_strategy']
    else:
        screener.strategy_name_dict['select_strategy']['xsz_strategy'] = {}


    print(TODAY, 'today ', type(TODAY))
    print(filter_date, type(filter_date))
    print('strategy_name_dict', screener.strategy_name_dict)
    
    # 应用筛选条件获取股票数据
    stocks = screener.select(date2int(filter_date))

    res = []
    c2 = time.time()
    print('c2 = ', c2 - c1)
    is_st_dg = screener.dm.is_st(screener.stock_list, date2int(filter_date))
    close_dg = screener.dm.close(screener.stock_list, date2int(filter_date), adj=False)
    total_mv = screener.dm.total_mv(screener.stock_list, date2int(filter_date)) * 1e4
    net_assets = screener.dm.net_assets(screener.stock_list, date2int(filter_date))
    goodwill = np.nan_to_num(screener.dm.goodwill(screener.stock_list, date2int(filter_date)))
    profit_dedtQ = screener.dm.profit_dedtQ(screener.stock_list, date2int(filter_date))



    c3 = time.time()
    print('c3 = ', c3 - c2)
    avg_total_mv = []
    index = 1 
    for code, score in stocks[:30]:
        idx = np.where(screener.stock_list == code)[0][0]
        # print(idx, is_st_dg.shape, is_st_dg[idx])
        data = pro.stock_basic(ts_code=change_ts(str(code)), fields='ts_code,symbol,name,exchange')
        avg_total_mv.append(total_mv[idx])
        res.append({'index': index, 'score': score, 'id': str(code), 'name': data.name.values[0], 'exchange_name': exchange_name_dict[data.exchange.values[0]], 
        'is_st': bool(is_st_dg[idx]), 'closing_price': close_dg[idx], 
        'market_cap': total_mv[idx] / 1e8, 
        'quarterly_pe': total_mv[idx] / profit_dedtQ[idx] / 4, 
        'quarterly_net_profit': profit_dedtQ[idx] / 1e8, 
        'goodwill': goodwill[idx] / 1e8 , 
        'net_assets': net_assets[idx] / 1e8, 
        'adjusted_pb': total_mv[idx] / (net_assets[idx] - goodwill[idx])})

        index += 1

    print('avg is ', np.mean(avg_total_mv[:20]))
    # 准备交易所筛选数据
    exchange_data = get_exchange_filter()
    
    print(res)

    c4 = time.time()
    print('c4 = ', c4 - c3)
    # 渲染结果页面
    return render_template('result.html', 
                           stocks=res,
                           filter_date=filter_date,
                           exclude_exchanges=exclude_exchanges,
                           exclude_st=exclude_st,
                           filter_pe_gt_zero=filter_pe_gt_zero,
                           sort_by=sort_by,
                           exchange_data=exchange_data)


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
# @no_cache
def run_backtest():
    # 获取筛选条件和回测参数
    # try:
        bs = Backtester('strategy_config.json.template')

        data = request.get_json()
        filter_conditions = data.get('filter', {})
        backtest_params = data.get('params', {})
        
        print('data is ', data)
        print('backtest params ', backtest_params)
        print('name dict is ', bs._screener.strategy_name_dict)

        exclude_exchanges = filter_conditions.get('exclude_exchanges', [])
        exclude_st = 'exclude_st' in filter_conditions
        filter_pe_gt_zero = 'filter_pe_gt_zero' in filter_conditions

        mapping = {
                'main': 'zb_strategy',
                'kcb': 'kcb_strategy',
                'cyb': 'cyb_strategy',
                'bse': 'bse_strategy'
            }
        for k in ['main', 'kcb', 'cyb', 'bse']:
            if k in exclude_exchanges:
                bs._screener.strategy_name_dict['select_strategy'][mapping[k]]['exclude'] = True
            else:
                bs._screener.strategy_name_dict['select_strategy'][mapping[k]]['exclude'] = False

        bs._screener.strategy_name_dict['select_strategy']['st_strategy']['exclude'] = exclude_st

        if not filter_pe_gt_zero:
            del  bs._screener.strategy_name_dict['select_strategy']['xsz_strategy']
        else:
            bs._screener.strategy_name_dict['select_strategy']['xsz_strategy'] = {}
        
        start_date = date2int(backtest_params['start_date'])
        end_date = date2int(backtest_params['end_date'])
        hold_num = int(backtest_params['hold_stocks'])
        rebalance_period = int(backtest_params['rebalance_period'])

        bs._screener.strategy_name_dict['trade_strategy']['start_time'] = str(start_date)
        bs._screener.strategy_name_dict['trade_strategy']['end_time'] = str(end_date)
        bs._screener.strategy_name_dict['trade_strategy']['holding_period'] = rebalance_period
        bs._screener.strategy_name_dict['trade_strategy']['max_stock_num'] = hold_num

        print('name dict2 is ', bs._screener.strategy_name_dict)
        res = bs.backtesting()
        
        for k in ['sharpe_ratio', 'annual_return', 'max_drawdown']:
            res[k] = float(res[k])
        print('my res is ', res)

        

        # if filter_conditions['filt']
        
        # 实际项目中这里会调用真实回测引擎
        # 这里生成模拟回测数据
        result = generate_backtest_results(backtest_params)
        
        print(result)
        return jsonify(res)

    # except Exception as e:
    #     print('error is ', e)
    #     return jsonify({
    #         "error": str(e),
    #         "message": "回测过程中发生错误"
    #     }), 500


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
    app.run('0.0.0.0')
