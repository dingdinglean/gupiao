# 美股日线 / 周月抄底雷达

这是一个完全独立运行的美股 DXDX 回调雷达：主雷达只在中长期多头趋势中寻找 **official daily DXDX** 日线底背离/回调结束信号。它不读取、不依赖任何其他仓库，也不使用 RSI 强势、板块排名或 watchlist 逻辑。

## RTH 数据原则

项目所有生产技术分析周期只使用美东时间 **09:30–16:00** 的美股正常交易时段（RTH）：日线、周线、月线均排除盘前、盘后、隔夜和其他 extended-hours 数据。日线以 `prepost=False` 获取并做防御性 RTH 确认；周/月只从这些 RTH 日K重采样，因此所有生产 OHLCV 和 DXDX、MACD、EMA 指标均基于正常交易时段成交。

## 信号规则

先决条件：最新完整日 K 的蓝梯（EMA23 通道）高于黄梯（EMA89 通道）。默认使用宽松的 `BLUE_ABOVE_YELLOW`；可设置 `STRICT_BLUE_ABOVE=true` 以要求蓝梯下沿高于黄梯上沿。

- **日线抄底**：Yahoo official `interval=1d` 日K出现 DXDX，且日线蓝梯 > 黄梯。
- 当天 official daily 尚未最终化时不产生信号；下一个 confirmation 会在最近 5 个完成的 XNYS session 中补确认。
- 生产主雷达不扫描、不发送也不记录 4H 信号；旧 4H 历史状态仅保留作历史去重记录。

DXDX 原公式、MACD 底背离逻辑、EMA23/EMA89 和完整收盘日K判断均保留；主雷达日线信号只使用 `auto_adjust=True` 的 Yahoo official 日K。

## 股票池与市场过滤

股票池仅为 **S&P 500 + Nasdaq-100**，去重后扫描。仅保留 NYSE/NASDAQ 上市普通股与美国 ADR；不接受港股、A 股、OTC、ETF、基金、指数、加密资产或非美国交易所代码。单只取数失败只会记录失败，不会终止全市场扫描。

## 运行与去重

GitHub Actions 在 UTC **22:30、周一至周五**运行一次，并保留 `workflow_dispatch`。`alert_state.json` 以 `symbol + timeframe + signal_bar_timestamp` 为键：同一根 K 永远只通知一次，新 K 的新 DXDX 允许再次通知。

无新信号时不会发邮件，日志会输出：`No new DXDX signals; email skipped.`，工作流仍成功。

## Gmail SMTP Secrets

所有邮件配置只通过 GitHub Actions Secrets 或本地 `.env` 注入：

- `SMTP_HOST`（Gmail：`smtp.gmail.com`）
- `SMTP_PORT`（Gmail STARTTLS：`587`）
- `SMTP_USER`
- `SMTP_PASSWORD`（Gmail App Password）
- `EMAIL_TO`

仓库中不保存邮箱地址、密码或授权码。Gmail 587 使用 STARTTLS，临时失败会重试一次。

## 输出

每次扫描生成并上传 artifact：

- `output/dxdx_signals.csv`
- `output/dxdx_report.txt`

CSV 保留兼容列，同时将新信号标记为 `signal_level=DAILY`、`source_radar=daily`、`source_timeframe=daily`，且 `h4_dxdx=False`。TXT 汇总股票池、成功/失败取数、日线信号数量和邮件是否发送。主工作流只上传日线 diagnostics。

## 本地验证

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python main.py --dry-run
```

可使用 `python main.py --test-email` 单独测试 Gmail 通道。

## 周线 / 月线大周期雷达

`long_main.py` 是与上述日线主雷达完全独立的周/月 DXDX 扫描器。它仍扫描同一份 S&P 500 + Nasdaq-100 美股普通股/美国 ADR 股票池，但从 `fetch_daily(period="max")` 获取日线后，在美东时区自行重采样为 Friday-labelled 周 K 和自然月月 K；不依赖 Yahoo 的未完成周/月 K。

- 周线仅在周五 16:20 ET 后使用当周 K；其他时间只检查上一根完整周 K。
- 月线仅使用已结束并经下一个交易日收盘确认的自然月 K，绝不使用正在形成的当月 K。
- 周/月仅判断原版 DXDX MACD 底背离，**不附加**日线蓝梯 > 黄梯过滤。
- `long_alert_state.json` 使用 `SYMBOL|TIMEFRAME|SIGNAL_BAR_TIMESTAMP` 键并保留 400 天，因此同一股票的周线和月线信号分别去重。

工作流 `.github/workflows/long_screen.yml` 名为 **US Weekly Monthly DXDX Pullback Radar**，在 UTC 23:10 的每个工作日运行，也可通过 `workflow_dispatch` 选择 dry run。它会上传：

- `output/long_dxdx_signals.csv`
- `output/long_dxdx_report.txt`

正式邮件成功后的周/月 CSV 是推送事实记录：只含该封邮件实际发送的 `new_signals`，并同时记录 DXDX K 的 `signal_price`、实际邮件市场日 `push_date` 与当时最新完整 RTH 日K收盘 `push_price`。dry run 仍输出全部扫描结果供核验。

本地 dry run：

```bash
python long_main.py --dry-run
```

仅供研究参考，不构成投资建议。
