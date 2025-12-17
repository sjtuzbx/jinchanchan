from backtest.backtester import Backtester
from services.utils import date2int, convert_numpy_types
from logger import Logger


class BacktestService:
    def __init__(self, config_template, name_cache):
        self.config_template = config_template
        self.name_cache = name_cache

    def _apply_filters(self, screener_dict, filter_conditions):
        exclude_exchanges = filter_conditions.get('exclude_exchanges', [])
        exclude_st = 'exclude_st' in filter_conditions
        filter_pe_gt_zero = 'filter_pe_gt_zero' in filter_conditions

        mapping = {
            'main': 'zb_strategy',
            'kcb': 'kcb_strategy',
            'cyb': 'cyb_strategy',
            'bse': 'bse_strategy'
        }
        for key, strategy_name in mapping.items():
            screener_dict['select_strategy'][strategy_name]['exclude'] = key in exclude_exchanges

        screener_dict['select_strategy']['st_strategy']['exclude'] = exclude_st

        if not filter_pe_gt_zero:
            screener_dict['select_strategy'].pop('xsz_strategy', None)
        else:
            screener_dict['select_strategy']['xsz_strategy'] = screener_dict['select_strategy'].get('xsz_strategy', {})

    def run(self, filter_conditions, backtest_params):
        bs = Backtester(self.config_template)
        self._apply_filters(bs._screener.strategy_name_dict, filter_conditions)

        start_date = date2int(backtest_params['start_date'])
        end_date = date2int(backtest_params['end_date'])
        hold_num = int(backtest_params['hold_stocks'])
        rebalance_period = int(backtest_params['rebalance_period'])

        trade_strategy = bs._screener.strategy_name_dict['trade_strategy']
        trade_strategy['start_time'] = str(start_date)
        trade_strategy['end_time'] = str(end_date)
        trade_strategy['holding_period'] = rebalance_period
        trade_strategy['max_stock_num'] = hold_num

        Logger.info(f"Running backtest from {start_date} to {end_date}, hold {hold_num}")
        result = bs.backtesting()
        result = convert_numpy_types(result)
        if self.name_cache:
            result = self.name_cache.enrich_result(result)
        for key in ['sharpe_ratio', 'annual_return', 'max_drawdown']:
            if key in result:
                result[key] = float(result[key])

        # from IPython import embed
        # embed()
        return result
