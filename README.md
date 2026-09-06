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

仅供研究参考，不构成投资建议。
