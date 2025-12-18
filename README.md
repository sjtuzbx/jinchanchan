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


## 20251218
dded a dedicated BasisHistory cache (services/basis_history.py:12-150) that pulls the past five years of主力合约数据 via TuShare, computes each日的年化贴水，并写入 data/futures_basis_history.json; it also keeps sorted distributions so we can query percentiles quickly. app.py (lines 26-52) now instantiates this cache and hands it to the monitor service (首次启动会花一两分钟完成数据拉取，之后每天只增量加载)。
实时监控的服务端已接入该缓存：services/monitor_service.py (lines 15-258) 记录了开/高/低，并在 _serialize_contract 中把最新年化贴水的历史分位数附加到响应里，用于前端显示。
监控页面新增“5年分位”列（templates/monitor.html:77-153），在每条合约旁实时显示最新年化贴水在过去五年的百分位；无历史数据时会自动显示 —。表空时的 colspan 也同步调整。
新生成的数据文件 data/futures_basis_history.json 保存了全部主力合约的年化贴水序列，可直接作为离线分析的输入。
Tests

python - <<'PY' from app import basis_history; print('done load', bool(basis_history.data)); ... PY
python - <<'PY' from app import monitor_service; payload = monitor_service.get_monitor_payload(); ... PY