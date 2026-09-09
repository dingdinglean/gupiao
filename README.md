# 美股双周期抄底雷达

这是一个完全独立运行的美股 DXDX 回调雷达：在中长期多头趋势中，寻找日线或 4H 级别出现的 DXDX 底背离/回调结束信号。它不读取、不依赖任何其他仓库，也不使用 RSI 强势、板块排名或 watchlist 逻辑。

## 信号规则

先决条件：最新完整日 K 的蓝梯（EMA23 通道）高于黄梯（EMA89 通道）。默认使用宽松的 `BLUE_ABOVE_YELLOW`；可设置 `STRICT_BLUE_ABOVE=true` 以要求蓝梯下沿高于黄梯上沿。

- **S级：双周期共振**：最新完整日 K 出现 DXDX，且当天最近两根完整 4H K 中至少一根出现 DXDX。
- **A级：4H 抄底**：日线趋势有效，且当天最近两根完整 4H K 中至少一根出现 DXDX。
- **B级：日线抄底**：日线趋势有效，且最新完整日 K 出现 DXDX。

DXDX 原公式、MACD 底背离逻辑、EMA23/EMA89、日线、4H 重采样和完整收盘 K 判断均保留。4H 不检查未收盘 K，也不会用前几天的旧信号再次触发。

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

CSV 包含信号等级、日线/4H DXDX、各自信号 K 时间、收盘价、趋势状态和检测时间。TXT 汇总股票池、成功/失败取数、S/A/B 数量和邮件是否发送。

## 本地验证

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python main.py --dry-run
```

可使用 `python main.py --test-email` 单独测试 Gmail 通道。

## 周线 / 月线大周期雷达

`long_main.py` 是与上述日线 + 4H 雷达完全独立的周/月 DXDX 扫描器。它仍扫描同一份 S&P 500 + Nasdaq-100 美股普通股/美国 ADR 股票池，但从 `fetch_daily(period="max")` 获取日线后，在美东时区自行重采样为 Friday-labelled 周 K 和自然月月 K；不依赖 Yahoo 的未完成周/月 K。

- 周线仅在周五 16:20 ET 后使用当周 K；其他时间只检查上一根完整周 K。
- 月线仅使用已结束并经下一个交易日收盘确认的自然月 K，绝不使用正在形成的当月 K。
- 周/月仅判断原版 DXDX MACD 底背离，**不附加**日线蓝梯 > 黄梯过滤。
- `long_alert_state.json` 使用 `SYMBOL|TIMEFRAME|SIGNAL_BAR_TIMESTAMP` 键并保留 400 天，因此同一股票的周线和月线信号分别去重。

工作流 `.github/workflows/long_screen.yml` 名为 **US Weekly Monthly DXDX Pullback Radar**，在 UTC 23:10 的每个工作日运行，也可通过 `workflow_dispatch` 选择 dry run。它会上传：

- `output/long_dxdx_signals.csv`
- `output/long_dxdx_report.txt`

本地 dry run：

```bash
python long_main.py --dry-run
```

仅供研究参考，不构成投资建议。
