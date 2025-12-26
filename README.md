# jinchanchan

#### 介绍
{**以下是 Gitee 平台说明，您可以替换此简介**
Gitee 是 OSCHINA 推出的基于 Git 的代码托管平台（同时支持 SVN）。专为开发者提供稳定、高效、安全的云端软件开发协作平台
无论是个人、团队、或是企业，都能够用 Gitee 实现代码托管、项目管理、协作开发。企业项目请看 [https://gitee.com/enterprises](https://gitee.com/enterprises)}

#### 软件架构
软件架构说明


#### 安装教程

1.  xxxx
2.  xxxx
3.  xxxx

#### 使用说明

1.  xxxx
2.  xxxx
3.  xxxx

#### 参与贡献

1.  Fork 本仓库
2.  新建 Feat_xxx 分支
3.  提交代码
4.  新建 Pull Request


#### 特技

1.  使用 Readme\_XXX.md 来支持不同的语言，例如 Readme\_en.md, Readme\_zh.md
2.  Gitee 官方博客 [blog.gitee.com](https://blog.gitee.com)
3.  你可以 [https://gitee.com/explore](https://gitee.com/explore) 这个地址来了解 Gitee 上的优秀开源项目
4.  [GVP](https://gitee.com/gvp) 全称是 Gitee 最有价值开源项目，是综合评定出的优秀开源项目
5.  Gitee 官方提供的使用手册 [https://gitee.com/help](https://gitee.com/help)
6.  Gitee 封面人物是一档用来展示 Gitee 会员风采的栏目 [https://gitee.com/gitee-stars/](https://gitee.com/gitee-stars/)


## 202512117

只在调仓日生成明细：code/backtest/backtest/backtester.py 现在引入 JC_DEBUG 环境开关（顶端）和 debug_print，所有原先的 print 改为受控输出，并且只在 date in dates_holding_period 时向 position_history 添加记录，trade_history 仍仅包含调仓动作。前端表格因此只展示调仓日的数据，而交易层数据依旧完整（对无交易的调仓日会展示持仓快照，文件同处: code/backtest/backtest/backtester.py:166-340）。
日志目录统一：把根目录下所有 test.log* 移至 log/，并更新 logger.py（logger.py (lines 118-121)）默认写入 Path(__file__).resolve().parent / "log"，保证之后的日志轮转都落在该文件夹。若旧文件仍在，已经集中到 log/ 可直接查看。
Debug 参数控制输出：新增 JC_DEBUG 环境变量及 debug_print。app.py (lines 23-52) 导入 services.utils/services.name_cache/services.backtest_service 并定义 DEBUG_MODE，随后所有原先的 print（筛选、性能计时代码等）都改为 debug_print（app.py (lines 420-513)），后端回测器也使用相同开关。默认运行无额外 stdout；若需要调试，可在启动前 export JC_DEBUG=1。
Stock 名称缓存 & 服务分层：在 services/ 下新增 utils.py, name_cache.py, backtest_service.py。StockNameCache 首次启动会利用 Tushare 把全量代码→名称写入 jinchanchan/data/stock_names.json 并复用；BacktestService 封装参数解算与结果富集，app.py 里 /backtest 路由仅做调度。
如需验证：1) 运行回测，持仓/交易表只列重平衡日；2) 查看 log/test.log 确认新日志；3) 若要查看调试输出，执行 JC_DEBUG=1 python app.py.


## 20251218
dded a dedicated BasisHistory cache (services/basis_history.py:12-150) that pulls the past five years of主力合约数据 via TuShare, computes each日的年化贴水，并写入 data/futures_basis_history.json; it also keeps sorted distributions so we can query percentiles quickly. app.py (lines 26-52) now instantiates this cache and hands it to the monitor service (首次启动会花一两分钟完成数据拉取，之后每天只增量加载)。
实时监控的服务端已接入该缓存：services/monitor_service.py (lines 15-258) 记录了开/高/低，并在 _serialize_contract 中把最新年化贴水的历史分位数附加到响应里，用于前端显示。
监控页面新增“5年分位”列（templates/monitor.html:77-153），在每条合约旁实时显示最新年化贴水在过去五年的百分位；无历史数据时会自动显示 —。表空时的 colspan 也同步调整。
新生成的数据文件 data/futures_basis_history.json 保存了全部主力合约的年化贴水序列，可直接作为离线分析的输入。
Tests

python - <<'PY' from app import basis_history; print('done load', bool(basis_history.data)); ... PY
python - <<'PY' from app import monitor_service; payload = monitor_service.get_monitor_payload(); ... PY


## 20251226
实时监控新增日内波动率走势：后端在 services/vix_intraday.py 持久化每次刷新得到的 VIX 快照，/monitor/data 返回 vix_intraday；前端 templates/monitor.html 增加“实时波动率走势”折线图并可切换 IH/IF/IC/IM。
日内走势只在交易时段采样：9:30-11:30、13:00-15:00 之外不写入数据点。
修复期权期限结构的 IV 计算：services/option_vix_service.py 改为从对应 ETF 实时行情获取标的价（510050/510300/510500/588000），不再误用期权字段作为标的价，避免 call/put IV 失真。
