# R010 Phase 1：同向最多3仓

运行：R010P1-REPRODUCTION-20261016T054014Z-082d89ae；范围：development_implementation_diagnostic_not_extension_acceptance。

区间：2023-01-01T00:00:00Z 至 2024-07-01T00:00:00Z（不含结束点）。
候选仅在SIG-001上加同向3仓限制；原信号、止损、退出、成本、资金与风险预算不变。
按原固定币种顺序接受信号，预留名额计入限制；成交检查排除当前订单。拒单释放名额，但不回补同批已拒绝的信号；计划退出成交前仍占位。

2026历史未读取。当前币池存在幸存者偏差；历史订单过滤器和维保规则仍为代理。分钟不利路径回撤不保证覆盖真实路径，停机后持有现金不等于持续交易稳定。

| 策略 | 成本 | 期末USDT | 累计收益 | 窗口折算年化 | 不利路径最大回撤 | 交易数 | 代理强平／歧义 |
|---|---|---:|---:|---:|---:|---:|---:|
| SIG-001同期对照 | base | 94.1756 | -5.82% | -3.93% | 13.76% | 61 | 1／1 |
| SIG-001同期对照 | stress | 92.8840 | -7.12% | -4.81% | 13.80% | 61 | 1／1 |
| 同向最多3仓 | base | 94.1756 | -5.82% | -3.93% | 13.76% | 61 | 1／1 |
| 同向最多3仓 | stress | 92.8840 | -7.12% | -4.81% | 13.80% | 61 | 1／1 |

未获得扩展验证通过资格；不启动Phase 2。
旧开发窗口仅作实现与复现诊断，不能替代2024H2–2025扩展验证。

净收益为正、至少30笔、同期年化改善≥1个百分点、回撤≤20%、无停机/代理强平/歧义、Calmar不降、14天配对循环区块重采样2000次改善区间下限>0，两种成本均需通过。重采样未修正多轮选择偏差，不是未来盈利概率。

- base：未通过项目['positive_net', 'minimum_cagr_improvement', 'no_halts_liquidations_ambiguities', 'bootstrap_lower_positive']；停机原因['liquidation_required:AVAXUSDT']；同向名额拒单{'R010P1:signal_same_direction_cap': 2}。
- stress：未通过项目['positive_net', 'minimum_cagr_improvement', 'no_halts_liquidations_ambiguities', 'bootstrap_lower_positive']；停机原因['liquidation_required:AVAXUSDT']；同向名额拒单{'R010P1:signal_same_direction_cap': 2}。

SIG-001旧窗口逐笔交易和每日权益复现：{'base_trades': True, 'base_daily_equity': True, 'stress_trades': True, 'stress_daily_equity': True}。
仓位名额审计：{'candidate_cap3_base': {'maximum_observed_same_direction': 2, 'pending_slots_at_end': 0}, 'candidate_cap3_stress': {'maximum_observed_same_direction': 2, 'pending_slots_at_end': 0}}。
