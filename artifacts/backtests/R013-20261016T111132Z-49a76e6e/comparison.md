# R013：持仓期限、止损宽度与入场确认的独立实验

三个候选在回测前分别锁定；没有组合、按币种或多空挑规则。全部历史已被观察，结果只是探索诊断。
连续期：2024-07-01 至 2026-01-01（不含结束点），两个月预热、共享初始 100 USDT、真实资金费 1 倍。

| 方案 | 成本 | 年化 | 不利路径回撤 | 交易数 | 净盈利胜率 | 平均净 R | 24h方向命中率 |
|---|---|---:|---:|---:|---:|---:|---:|
| control | base | 6.81% | 8.68% | 122 | 27.87% | 0.1629 | 48.76% |
| control | stress | 5.00% | 8.50% | 121 | 27.27% | 0.0982 | 47.50% |
| hold168 | base | 1.73% | 8.48% | 114 | 27.19% | -0.0054 | 48.67% |
| hold168 | stress | 1.21% | 8.36% | 112 | 27.68% | -0.0161 | 47.75% |
| stop3atr | base | 4.01% | 7.64% | 97 | 30.93% | 0.1499 | 46.88% |
| stop3atr | stress | 2.60% | 7.76% | 95 | 30.53% | 0.0845 | 47.87% |
| confirm2 | base | -0.07% | 3.91% | 15 | 26.67% | 0.0263 | 64.29% |
| confirm2 | stress | -0.38% | 3.76% | 14 | 28.57% | -0.0039 | 61.54% |

hold168：只将最长期限改为 168 小时；stop3atr：只将初始止损改为 3ATR、原风险预算下自动减仓；confirm2：连续两根小时原信号同方向合格再开仓。
胜率为净盈利交易比例；24h方向标签为实际成交样本的固定期限原始价格方向，不含费用、不参与交易。样本随策略路径改变，不能单独视为因果预测提升。
R 为净盈亏/初始价格止损风险；净利润已扣费用和资金费，R 分母不含费用缓冲，也不是严格最大损失保证。

## 预设门槛及半年诊断

### hold168：探索门槛通过 False

- spot/base：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-13.10, +0.63] 个百分点；半年改善 0/6，半年风险全部通过 False。
- spot/stress：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-11.29, +1.57] 个百分点；半年改善 0/6，半年风险全部通过 False。
- ffill/base：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-13.10, +0.63] 个百分点；半年改善 0/6，半年风险全部通过 False。
- ffill/stress：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-11.29, +1.57] 个百分点；半年改善 0/6，半年风险全部通过 False。

| 半年 | 成本 | 原策略累计收益 | 候选累计收益 | 候选风险门槛 |
|---|---|---:|---:|---|
| 2023H1 | base | -8.55% | -8.81% | True |
| 2023H2 | base | +2.87% | +2.13% | False |
| 2024H1 | base | -7.95% | -8.77% | True |
| 2024H2 | base | +1.48% | -1.42% | True |
| 2025H1 | base | +8.81% | +5.45% | True |
| 2025H2 | base | +1.17% | -0.55% | True |
| 2023H1 | stress | -8.80% | -9.04% | True |
| 2023H2 | stress | +1.72% | +1.03% | False |
| 2024H1 | stress | -8.37% | -9.03% | True |
| 2024H2 | stress | +0.00% | -1.58% | True |
| 2025H1 | stress | +8.40% | +4.67% | True |
| 2025H2 | stress | +0.65% | -0.61% | True |

### stop3atr：探索门槛通过 False

- spot/base：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-10.65, +3.65] 个百分点；半年改善 3/6，半年风险全部通过 False。
- spot/stress：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-10.51, +4.02] 个百分点；半年改善 4/6，半年风险全部通过 True。
- ffill/base：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-10.65, +3.65] 个百分点；半年改善 3/6，半年风险全部通过 False。
- ffill/stress：未通过 ['cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-10.51, +4.02] 个百分点；半年改善 4/6，半年风险全部通过 True。

| 半年 | 成本 | 原策略累计收益 | 候选累计收益 | 候选风险门槛 |
|---|---|---:|---:|---|
| 2023H1 | base | -8.55% | -4.91% | True |
| 2023H2 | base | +2.87% | +1.51% | False |
| 2024H1 | base | -7.95% | -6.32% | True |
| 2024H2 | base | +1.48% | +0.17% | True |
| 2025H1 | base | +8.81% | +4.85% | True |
| 2025H2 | base | +1.17% | +1.54% | True |
| 2023H1 | stress | -8.80% | -5.17% | True |
| 2023H2 | stress | +1.72% | +3.87% | True |
| 2024H1 | stress | -8.37% | -7.51% | True |
| 2024H2 | stress | +0.00% | -0.83% | True |
| 2025H1 | stress | +8.40% | +3.86% | True |
| 2025H2 | stress | +0.65% | +1.12% | True |

### confirm2：探索门槛通过 False

- spot/base：未通过 ['positive_net', 'minimum_trades', 'cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-23.56, +5.69] 个百分点；半年改善 3/6，半年风险全部通过 True。
- spot/stress：未通过 ['positive_net', 'minimum_trades', 'cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-20.90, +6.30] 个百分点；半年改善 3/6，半年风险全部通过 True。
- ffill/base：未通过 ['positive_net', 'minimum_trades', 'cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-23.56, +5.69] 个百分点；半年改善 3/6，半年风险全部通过 True。
- ffill/stress：未通过 ['positive_net', 'minimum_trades', 'cagr_improvement', 'calmar_non_decreasing', 'bootstrap_lower_positive', 'mean_net_R_improvement']；年化改善 95% 区间 [-20.90, +6.30] 个百分点；半年改善 3/6，半年风险全部通过 True。

| 半年 | 成本 | 原策略累计收益 | 候选累计收益 | 候选风险门槛 |
|---|---|---:|---:|---|
| 2023H1 | base | -8.55% | +0.00% | True |
| 2023H2 | base | +2.87% | +0.20% | True |
| 2024H1 | base | -7.95% | +0.14% | True |
| 2024H2 | base | +1.48% | -2.24% | True |
| 2025H1 | base | +8.81% | -0.47% | True |
| 2025H2 | base | +1.17% | +2.61% | True |
| 2023H1 | stress | -8.80% | +0.00% | True |
| 2023H2 | stress | +1.72% | +0.08% | True |
| 2024H1 | stress | -8.37% | -1.65% | True |
| 2024H2 | stress | +0.00% | -2.15% | True |
| 2025H1 | stress | +8.40% | -0.47% | True |
| 2025H2 | stress | +0.65% | +2.01% | True |

所有半年各重置 100 USDT，不拼接收益，也不是独立滚动样本外。2026 未读取。
普通 bootstrap 没有修正本轮多候选和此前多轮选择偏差；正式选模资格始终 false，不自动采用或组合候选。
共 9 次、72 个账户，3 个机制、6 个候选成本配置，输入重复在回测前按指纹去重。

[事前方案](../../../docs/第十三轮预注册方案.md)
[完整汇总](summary.json)


## 数学期望与实现核对

| 基础成本方案 | 平均盈利 USDT | 平均亏损绝对值 USDT | 每笔平均净收益 USDT |
|---|---:|---:|---:|
| 原策略 | 1.3051 | 0.3859 | +0.0853 |
| 持仓上限 168h | 1.0538 | 0.3621 | +0.0230 |
| 初始止损 3ATR | 0.8369 | 0.2839 | +0.0628 |
| 两根小时确认 | 0.9862 | 0.3677 | -0.0066 |

宽止损下仓位按同一风险预算减少；本次胜率从 27.87% 升至 30.93%，但平均盈利和每笔净收益下降，不能只据胜率决定采用。不同策略的成交和资金路径也不同，以上不是同一批交易简单移动止损的静态比较。

双确认基础成本共 15 笔交易，只有 14 笔有窗口内完整的 24h 标签，其中 9 笔方向为正；这 9 笔里有 5 笔实际仍净亏（3 笔止损、2 笔均线退出）。方向终点正确并不保证中途能按现有规则盈利兑现。压力成本只有 13 个标签、8 次方向为正，样本不足以证明可重复的预测提升。没有读取窗口之外或 2026 的价格补标签。

延长持仓只改变硬性期限，仍保留 EMA50 退出，不代表测试了所有大周期趋势退出方法。本候选六个半年两种成本均无收益改善；不根据某笔事后大趋势继续调整天数。平均净 R 与平均净 USDT 使用不同权重，符号不必一致；净 R 分母和每笔计算已单独记录并核对。

15 个相关测试通过（本轮 8、Phase 1 7）。全部九个窗口的原策略交易、每日和小时权益逐字节复现上一轮对照；原金融、信号和名额核心源文件保持不变。72 个账户的风险配置、每笔初始 R、数学期望恒等式、固定期限方向标签均通过核对。两套代理在需要分别回放的窗口下，所有账户交易及权益路径逐字节相同。

三项均未通过探索门槛，不替换基准、不自动组合。全部原始结果保留，后续任何研究需重新立项；当前历史结果不具备正式选模资格。

[源哈希、资金风险、逐笔 R 与方向标签审计](implementation_validation.json)
