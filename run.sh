202512117

只在调仓日生成明细：code/backtest/backtest/backtester.py 现在引入 JC_DEBUG 环境开关（顶端）和 debug_print，所有原先的 print 改为受控输出，并且只在 date in dates_holding_period 时向 position_history 添加记录，trade_history 仍仅包含调仓动作。前端表格因此只展示调仓日的数据，而交易层数据依旧完整（对无交易的调仓日会展示持仓快照，文件同处: code/backtest/backtest/backtester.py:166-340）。
日志目录统一：把根目录下所有 test.log* 移至 log/，并更新 logger.py（logger.py (lines 118-121)）默认写入 Path(__file__).resolve().parent / "log"，保证之后的日志轮转都落在该文件夹。若旧文件仍在，已经集中到 log/ 可直接查看。
Debug 参数控制输出：新增 JC_DEBUG 环境变量及 debug_print。app.py (lines 23-52) 导入 services.utils/services.name_cache/services.backtest_service 并定义 DEBUG_MODE，随后所有原先的 print（筛选、性能计时代码等）都改为 debug_print（app.py (lines 420-513)），后端回测器也使用相同开关。默认运行无额外 stdout；若需要调试，可在启动前 export JC_DEBUG=1。
Stock 名称缓存 & 服务分层：在 services/ 下新增 utils.py, name_cache.py, backtest_service.py。StockNameCache 首次启动会利用 Tushare 把全量代码→名称写入 jinchanchan/data/stock_names.json 并复用；BacktestService 封装参数解算与结果富集，app.py 里 /backtest 路由仅做调度。
如需验证：1) 运行回测，持仓/交易表只列重平衡日；2) 查看 log/test.log 确认新日志；3) 若要查看调试输出，执行 JC_DEBUG=1 python app.py.


python app.py
