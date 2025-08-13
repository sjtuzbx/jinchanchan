from flask import Flask, render_template, request, redirect, url_for
import json
import numpy as np
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

import tushare as ts

exchange_name_dict = {
    "BSE": "北交所",
    "SZSE": "深交所",
    "SSE": "上交所"
}

token = "1e266a5110f1d8fd926d3af0d034458b9d5c904636c72c723ab9fa38"
pro = ts.pro_api(token)

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
