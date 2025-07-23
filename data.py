import json
import random
from datetime import date, timedelta

# 生成模拟股票数据
def generate_stock_data(filter_date):
    exchanges = {
        "main": "主板", 
        "star": "科创板", 
        "gem": "创业板", 
        "bse": "北交所"
    }
    
    data = []
    
    # 生成模拟的股票数据
    stock_names = ["中国平安", "贵州茅台", "宁德时代", "招商银行", "隆基绿能", 
                  "药明康德", "比亚迪", "海康威视", "东方财富", "迈瑞医疗",
                  "金山办公", "中芯国际", "福昕软件", "艾迪药业", "寒武纪"]
    
    for i in range(100):
        exchange_code = random.choice(list(exchanges.keys()))
        stock_id = f"{600000 + i}.SH" if exchange_code == "main" else f"{300000 + i}.SZ"
        is_st = random.random() < 0.1  # 10%概率是ST股票
        
        # 生成随机财务数据
        closing_price = round(random.uniform(5, 500), 2)
        market_cap = round(random.uniform(1, 5000) * 100000000, 2)
        quarterly_pe = round(random.uniform(-20, 100), 2)
        goodwill = round(random.uniform(0, 100) * 100000000, 2)
        net_assets = round(random.uniform(50, 500) * 100000000, 2)
        quarterly_net_profit = round(random.uniform(-5, 50) * 100000000, 2)
        
        # 计算扣除商誉PB
        adjusted_net_assets = net_assets - goodwill
        if net_assets > 0:
            adjusted_pb = round(market_cap / net_assets, 2)
        else:
            adjusted_pb = quarterly_pe = -1  # 无效值
        
        data.append({
            "id": stock_id,
            "name": random.choice(stock_names),
            "exchange": exchange_code,
            "exchange_name": exchanges[exchange_code],
            "is_st": is_st,
            "closing_price": closing_price,
            "market_cap": market_cap,
            "quarterly_pe": quarterly_pe,
            "quarterly_net_profit": quarterly_net_profit,
            "goodwill": goodwill,
            "net_assets": net_assets,
            "adjusted_pb": adjusted_pb
        })
    
    return data

# 获取股票数据（实际应用中应替换为真实数据接口）
def get_stock_data(filter_date):
    # 实际项目中应使用API获取真实数据
    # 这里使用模拟数据
    return generate_stock_data(filter_date)

# 获取交易所筛选数据
def get_exchange_filter():
    return [
        {"code": "main", "name": "主板", "count": 0},
        {"code": "kcb", "name": "科创板", "count": 0},
        {"code": "cyb", "name": "创业板", "count": 0},
        {"code": "bse", "name": "北交所", "count": 0}
    ]


# 在现有基础上增加历史数据模拟功能
def generate_history_data(symbol, start_date, end_date):
    """生成指定股票的历史价格数据"""
    # 实际应用中应替换为真实数据
    history_data = []
    
    current_date = datetime.strptime(start_date, '%Y-%m-%d')
    end_date = datetime.strptime(end_date, '%Y-%m-%d')
    days = (end_date - current_date).days
    
    start_price = random.uniform(5, 200)
    
    for i in range(days):
        # 随机生成价格
        open_price = start_price * (1 + random.normalvariate(0, 0.01))
        close_price = open_price * (1 + random.normalvariate(0, 0.02))
        high = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.randint(1000000, 50000000)
        
        history_data.append({
            'date': current_date.strftime('%Y-%m-%d'),
            'open': round(open_price, 2),
            'close': round(close_price, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'volume': volume
        })
        
        current_date += timedelta(days=1)
        start_price = close_price
    
    return history_data