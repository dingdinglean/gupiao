# 美股双周期抄底选股推送

基于富图牛牛(Tongdaxin/通达信)公式翻译的美股选股推送工具。

## 选股条件

一只股票要被推送,必须**同时**满足:

1. **蓝梯 > 黄梯** —— 23周期 EMA 通道整体高于 89 周期 EMA 通道(多头排列)
2. **日线抄底** —— `DXDX` 在最近几根日 K 内首现
3. **4H 抄底** —— `DXDX` 在最近几根 4 小时 K 内首现

`DXDX` 是原公式里的"抄底首现"信号:MACD 出现底背离 `CCC`(普通或隐藏)之后,
DIF 的绝对值连续两根 K 收窄 ≥1%(结构 `JJJ` 出现的第一根)。两个时间级别同时触发
= 趋势共振,过滤掉单时间级别的假信号。

> 不构成投资建议,仅供研究参考。

---

## 项目结构

```
stock_screener/
├── indicators.py         # 富图牛牛公式翻译 (MACD背离 + 双EMA通道)
├── data_fetcher.py       # yfinance 取数 + 4H 重采样(锚定 09:30 ET)
├── universe.py           # S&P 500 + NASDAQ 100 票池 (24h Wikipedia 缓存)
├── screener.py           # 多线程跑批 + 双周期共振判断
├── notifier.py           # SMTP 邮件推送 (QQ / Gmail 通用)
├── state.py              # 去重,同一信号只推一次
├── main.py               # 入口 + 定时循环
├── test_indicators.py    # 指标 sanity check
├── requirements.txt
└── config.example.env
```

---

## 一、本地安装

```bash
# 推荐 Python 3.10+
pip install -r requirements.txt
```

跑指标自检(不需要联网,用合成数据):
```bash
python test_indicators.py
```
应该看到 `DXDX hits: 3`、`DBJGXC hits: 1` 类似的输出。

---

## 二、配置邮箱(关键步骤 ⚠️)

### 1. 复制配置文件

```bash
cp config.example.env .env
```

### 2. 获取 QQ 邮箱授权码

QQ 邮箱的 SMTP **不能用 QQ 密码登录**,必须用专门的"授权码":

1. 浏览器登录 https://mail.qq.com
2. 顶部 **设置 → 账户**
3. 往下滚动找到 **"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"**
4. 开启 **IMAP/SMTP 服务**(会让你发一条短信验证)
5. 验证完成后会生成一个 **16 位授权码**(类似 `abcdefghijklmnop`),复制下来
6. 把它粘进 `.env` 的 `SMTP_PASSWORD=`

`.env` 里默认已经填了你的邮箱 `728962465@qq.com`,只需要填授权码。

### 3. 干跑测试 (不真的发邮件)

```bash
python main.py --once --dry-run --symbols KO AAPL MSFT NVDA
```
应该看到扫描日志和 "DRY RUN - would have sent email..." 字样。

### 4. 真发一封测试邮件

```bash
python main.py --once --symbols KO AAPL MSFT NVDA
```

如果没命中,可以临时调宽窗口看看链路通不通:
```bash
DAILY_LOOKBACK_BARS=30 H4_LOOKBACK_BARS=30 python main.py --once --symbols KO
```

---

## 三、日常使用

### 单只股票测试

```bash
python main.py --once --symbols KO
```

### 扫一次 S&P 500

```bash
python main.py --once --universe sp500
```

### 守护进程模式(每 30 分钟扫一次,新信号自动发邮件)

```bash
python main.py
# 或自定义间隔
python main.py --interval-min 15
```

### 后台跑(Linux/Mac)

```bash
nohup python main.py > screener.log 2>&1 &
```

### 配合 cron(收盘后扫一次,每个交易日)

美东 16:30(对应美股收盘后 30 分钟),`crontab -e`:
```
30 16 * * 1-5 cd /path/to/stock_screener && /usr/bin/python main.py --once >> cron.log 2>&1
```

如果你在新加坡 / 中国时区,自己换算一下 UTC。美东收盘对应:
- 北京/新加坡时间 凌晨 4:00(冬令时)/ 凌晨 5:00(夏令时)
- 用 UTC 写 cron: `30 20 * * 1-5`(夏令时) / `30 21 * * 1-5`(冬令时)

---

## 四、参数调优

`.env` 里几个常用旋钮:

| 参数 | 默认 | 说明 |
|---|---|---|
| `STRICT_BLUE_ABOVE` | `false` | `true` 则要求蓝梯下沿>黄梯上沿(更严格,信号更少) |
| `DAILY_LOOKBACK_BARS` | `3` | 日线 DXDX 触发后多少根 K 之内仍算"新鲜" |
| `H4_LOOKBACK_BARS` | `2` | 4H 同上,2 根 ≈ 当前会话 |
| `MAX_WORKERS` | `8` | 并发线程,调高会被 Yahoo 限速 |

---

## 五、想加什么自己改

代码刻意写得平铺直叙,几个常见扩展点:

- **加港股/A 股**:`data_fetcher.py` 里 yfinance 改成 akshare/tushare,
  `universe.py` 改票池来源。指标层完全不动。
- **加更多信号**:`screener.check_symbol` 里把 `DXDX` 换成 `LLL`(早一步的底背离)
  或加 `DBJGXC` 做卖出推送。
- **换推送通道**:抄 `notifier.py` 写个 `notifier_telegram.py`,
  `main.py` 里换 import。
- **回测**:`indicators.add_all_indicators(df)` 已经把信号列加好,
  拿 `out["DXDX"]` 的索引就能做 vector backtest。

---

## 六、已知限制

- yfinance 的 1H 数据上限是 ~730 天,所以 4H 历史最多 ~2 年。
- yfinance 实时数据有 ~15 分钟延迟,所以"4H 抄底"实际是延迟 15min 触发。
  要做到真·实时,需要 IEX / Polygon / Alpaca 等付费数据源,换数据层即可。
- 4H bar 的时间锚点是 09:30 ET,所以每天有两根 4H bar:09:30–13:30、13:30–17:30(含盘后空 bar 已 drop)。
  不同平台 4H bar 定义略有差异,如果你看 moomoo 自己的 4H bar 想完全对齐,
  可能要调 `data_fetcher.resample_to_4h` 的 `origin`。

---

## 故障排查

**Q: 邮件发不出去,报 `Authentication failed`?**
A: 99% 是用了 QQ 密码而不是授权码。重新生成授权码粘贴一次。

**Q: 扫描很慢?**
A: 600 只 * 2 个时间级别 ≈ 1200 个 HTTP 请求。8 线程大概 6–12 分钟。
   想更快用 `MAX_WORKERS=16`,但太高会被 Yahoo 限速,得不偿失。

**Q: 一只都扫不出?**
A: 这个条件本身就很严格(双周期共振 + 多头排列 + 抄底首现),
   单日扫全 S&P + NDX 经常一只都没有。临时调大 `DAILY_LOOKBACK_BARS=10` 看看。

**Q: 怎么知道指标对不对?**
A: `python main.py --once --dry-run --symbols KO`,
   把命中日期跟你富图牛牛 app 里 KO 的图对一下,信号位置应该一致。
