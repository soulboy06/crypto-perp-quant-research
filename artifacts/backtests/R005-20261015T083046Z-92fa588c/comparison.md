# 第五轮因子研究 R005-v1

本轮没有通过完整改进门槛的因子，原研究基准不替换。

运行 `R005-20261015T083046Z-92fa588c`，八组回放 163.4 秒。2023—2025 年已观察开发期，2022年11—12月仅热身；每组100 USDT、BTC和ETH共享资金。2026未读取。所有收益均为手续费、滑点、实际资金费后。

## 与两个已保存版本比较

| 版本 | 基础年化 | 压力年化 | 基础回撤 | 压力回撤 | 基础／压力交易数 |
|---|---:|---:|---:|---:|---:|
| R002原版 | +4.78% | +3.24% | 9.83% | 9.59% | 186／161 |
| R004走势效率过滤 | +6.46% | +4.60% | 6.07% | 7.53% | 142／114 |
| 放量确认 | +5.96% | +3.60% | 9.19% | 8.80% | 190／159 |
| 主动买卖压力 | +5.41% | +5.35% | 6.43% | 5.69% | 115／104 |
| 收盘位置 | +2.50% | +2.83% | 10.78% | 9.89% | 123／106 |
| 资金费拥挤度 | +6.20% | +3.96% | 9.19% | 8.14% | 182／155 |

回撤是既定分钟不利标记价路径的观测值，不是所有真实路径或未来损失上限。R004只是已观察的较高成绩研究参照，未通过独立验证。本轮不只要求赢过较弱的原版。

## 运行前固定的四个单因子

1. 放量确认：当前完整小时USDT成交额÷此前24个小时平均成交额≥1；均值不包含当前小时，历史不足或均值为零拒绝。
2. 主动买卖压力：当前完整小时主动买入USDT成交额÷总USDT成交额，多头≥55%、空头≤45%；零成交额拒绝。主动买入是吃单方向，不是新增资金净流入，也不是盘口挂单不平衡。
3. 收盘位置：(小时收盘−小时最低)/(小时最高−小时最低)，多头≥75%、空头≤25%；小时无价格区间时拒绝。
4. 资金费拥挤度：只使用已结算且至少过去60秒的最新费率，按结算间隔换算到8小时。多头用正费率、空头用相反数，方向性费率≤0.01%才允许；超过24小时未更新或缺失拒绝。它既不读取下一次费率，也不保证下次资金费和价格方向。结算后60秒可用是研究假设，真实发布延迟未核实。

每次只在R002原有突破、EMA200和已闭合四小时方向条件后加一个入场过滤。没有叠加R004效率因子，没有改退出、仓位预算、72小时上限、费用或风险。只测试每因子一组研究默认值，不能把它们称作已建立的小时级加密货币风险溢价。

## 比较门槛与不确定性

相对R004效率过滤版，两种成本都须：年化至少增加1个百分点；回撤最多多2个百分点且≤20%；Calmar不下降；六个半年至少四个改善；配对14天循环区块重采样2000次（种子20261015）的年化改善区间下限>0；并保留原有收益和风险条件。预设门槛未因结果改变。

- 放量确认：未通过：base: 相对上轮过滤版年化提升不足1个百分点；base: 回撤超过门槛；base: 收益回撤比未保持；base: 改善半年不足4/6；base: 重采样未支持稳定改善；stress: 相对上轮过滤版年化提升不足1个百分点；stress: 收益回撤比未保持；stress: 改善半年不足4/6；stress: 重采样未支持稳定改善。
- 主动买卖压力：未通过：base: 相对上轮过滤版年化提升不足1个百分点；base: 收益回撤比未保持；base: 改善半年不足4/6；base: 重采样未支持稳定改善；stress: 相对上轮过滤版年化提升不足1个百分点；stress: 改善半年不足4/6；stress: 重采样未支持稳定改善。
- 收盘位置：未通过：base: 相对上轮过滤版年化提升不足1个百分点；base: 回撤超过门槛；base: 收益回撤比未保持；base: 改善半年不足4/6；base: 重采样未支持稳定改善；stress: 相对上轮过滤版年化提升不足1个百分点；stress: 回撤超过门槛；stress: 收益回撤比未保持；stress: 改善半年不足4/6；stress: 重采样未支持稳定改善。
- 资金费拥挤度：未通过：base: 相对上轮过滤版年化提升不足1个百分点；base: 回撤超过门槛；base: 收益回撤比未保持；base: 改善半年不足4/6；base: 重采样未支持稳定改善；stress: 相对上轮过滤版年化提升不足1个百分点；stress: 收益回撤比未保持；stress: 改善半年不足4/6；stress: 重采样未支持稳定改善。

| 账户 | 2023 | 2024 | 2025 | 胜过效率版的半年 | 年化改善区间（百分点） |
|---|---:|---:|---:|---:|---:|
| volume_confirm_base | -0.81% | +7.89% | +11.20% | 3/6 | [-4.12, +3.87] |
| volume_confirm_stress | +1.40% | +4.97% | +4.47% | 3/6 | [-4.90, +3.93] |
| taker_pressure_base | +2.29% | +8.35% | +5.69% | 2/6 | [-8.15, +5.44] |
| taker_pressure_stress | +4.64% | +5.31% | +6.11% | 3/6 | [-4.01, +5.87] |
| close_location_base | -1.55% | +4.91% | +4.27% | 2/6 | [-10.54, +1.55] |
| close_location_stress | -0.63% | +5.10% | +4.12% | 2/6 | [-5.67, +1.62] |
| funding_crowding_base | +0.11% | +8.84% | +9.95% | 3/6 | [-4.45, +4.47] |
| funding_crowding_stress | +2.31% | +5.03% | +4.57% | 3/6 | [-4.87, +4.23] |

重采样只是已观察开发样本的条件诊断，没有校正多轮研究和人为选择偏差，不能解释为未来盈利概率。分半年不重置本金、不重新训练；本轮未训练机器学习模型，未计算因子IC或PBO。

## 集中度、资金与拒单

- volume_confirm_base：期末 119.0012 USDT；交易分布 {"ETHUSDT": 188, "BTCUSDT": 2}；分币净盈亏 {"ETHUSDT": 17.0106026724069, "BTCUSDT": 1.990572003874289}；前五笔占全部盈利交易净收益之和 29.17%；账面扣最大盈利单后净盈亏 +13.0059 USDT；手续费 6.1188，资金费净额 -1.1065。拒单预算／订单门槛 {"reserve_rejected": 872, "rejected_binding:stop_risk": 872, "rejected_filter:below_min_notional": 872, "reserve_passed": 190, "rejected_filter:rounded_quantity_zero": 641, "rejected_filter:below_min_quantity": 641}；诊断不可用 0 条。
- volume_confirm_stress：期末 111.1935 USDT；交易分布 {"ETHUSDT": 159}；分币净盈亏 {"ETHUSDT": 11.193453351198357}；前五笔占全部盈利交易净收益之和 31.58%；账面扣最大盈利单后净盈亏 +7.3763 USDT；手续费 4.9170，资金费净额 -0.9929。拒单预算／订单门槛 {"reserve_rejected": 953, "rejected_binding:stop_risk": 953, "rejected_filter:below_min_notional": 953, "reserve_passed": 159, "rejected_filter:rounded_quantity_zero": 651, "rejected_filter:below_min_quantity": 651}；诊断不可用 0 条。
- taker_pressure_base：期末 117.1381 USDT；交易分布 {"ETHUSDT": 113, "BTCUSDT": 2}；分币净盈亏 {"ETHUSDT": 15.14753786264376, "BTCUSDT": 1.990572003874289}；前五笔占全部盈利交易净收益之和 36.77%；账面扣最大盈利单后净盈亏 +12.7899 USDT；手续费 3.9068，资金费净额 -0.5856。拒单预算／订单门槛 {"reserve_rejected": 397, "rejected_binding:stop_risk": 397, "rejected_filter:below_min_notional": 397, "reserve_passed": 115, "rejected_filter:rounded_quantity_zero": 302, "rejected_filter:below_min_quantity": 302}；诊断不可用 0 条。
- taker_pressure_stress：期末 116.9292 USDT；交易分布 {"ETHUSDT": 104}；分币净盈亏 {"ETHUSDT": 16.929173261927502}；前五笔占全部盈利交易净收益之和 40.08%；账面扣最大盈利单后净盈亏 +12.8575 USDT；手续费 3.3567，资金费净额 -0.5919。拒单预算／订单门槛 {"reserve_rejected": 411, "rejected_binding:stop_risk": 411, "rejected_filter:below_min_notional": 411, "reserve_passed": 104, "rejected_filter:rounded_quantity_zero": 306, "rejected_filter:below_min_quantity": 306}；诊断不可用 0 条。
- close_location_base：期末 107.6910 USDT；交易分布 {"ETHUSDT": 123}；分币净盈亏 {"ETHUSDT": 7.6909983752030096}；前五笔占全部盈利交易净收益之和 36.98%；账面扣最大盈利单后净盈亏 +4.3254 USDT；手续费 3.8087，资金费净额 -0.9319。拒单预算／订单门槛 {"reserve_rejected": 584, "rejected_binding:stop_risk": 584, "rejected_filter:below_min_notional": 584, "reserve_passed": 123, "rejected_filter:rounded_quantity_zero": 392, "rejected_filter:below_min_quantity": 392}；诊断不可用 0 条。
- close_location_stress：期末 108.7405 USDT；交易分布 {"ETHUSDT": 106}；分币净盈亏 {"ETHUSDT": 8.740522546675667}；前五笔占全部盈利交易净收益之和 40.55%；账面扣最大盈利单后净盈亏 +5.4190 USDT；手续费 3.1789，资金费净额 -0.7572。拒单预算／订单门槛 {"reserve_rejected": 611, "rejected_binding:stop_risk": 611, "rejected_filter:below_min_notional": 611, "reserve_passed": 106, "rejected_filter:rounded_quantity_zero": 397, "rejected_filter:below_min_quantity": 397}；诊断不可用 0 条。
- funding_crowding_base：期末 119.8097 USDT；交易分布 {"ETHUSDT": 180, "BTCUSDT": 2}；分币净盈亏 {"ETHUSDT": 17.8191543863973, "BTCUSDT": 1.990572003874289}；前五笔占全部盈利交易净收益之和 29.73%；账面扣最大盈利单后净盈亏 +13.8145 USDT；手续费 5.9433，资金费净额 -0.7758。拒单预算／订单门槛 {"reserve_rejected": 826, "rejected_binding:stop_risk": 826, "rejected_filter:below_min_notional": 826, "reserve_passed": 182, "rejected_filter:rounded_quantity_zero": 623, "rejected_filter:below_min_quantity": 623}；诊断不可用 0 条。
- funding_crowding_stress：期末 112.3693 USDT；交易分布 {"ETHUSDT": 155}；分币净盈亏 {"ETHUSDT": 12.369274914036763}；前五笔占全部盈利交易净收益之和 31.91%；账面扣最大盈利单后净盈亏 +8.5521 USDT；手续费 4.8330，资金费净额 -0.6730。拒单预算／订单门槛 {"reserve_rejected": 903, "rejected_binding:stop_risk": 903, "rejected_filter:below_min_notional": 903, "reserve_passed": 155, "rejected_filter:rounded_quantity_zero": 633, "rejected_filter:below_min_quantity": 633}；诊断不可用 0 条。

扣掉最大盈利单只是账面诊断，没有重新回放后续资金占用。预算限制与最小订单失败标志可能重叠，不能相加当成独立拒单数。

## 数据和研究边界

量能来自既有已验收官方原始K线ZIP，每币27768个小时，合计55536小时。旧标准化OHLC文件没有量能字段，因此本轮只新增提取并校验USDT成交额和主动买入USDT成交额、逐分钟时间连续性、小时完整性及买入额不超过总额，未重跑原始文件校验和扫描。新表、来源记录及哈希清单见本目录source/factor_manifest.json。原始历史数据和旧规范化文件保持不变。

累计已完成有效候选×成本配置24个（此前16、本轮8），另有首轮作废调试8个账户，全部保留。八组结果全部报告，不追加阈值搜索、因子组合、币种、杠杆或保留期评估。六项新增关键测试通过；各账户核对资金守恒、持仓期限、因子日志延迟与资金费可见性。

文献只作为研究方向依据：[Risks and Returns of Cryptocurrency](https://www.nber.org/papers/w24877)讨论动量和注意力；[Common Risk Factors in Cryptocurrency](https://www.nber.org/papers/w25882)研究较大横截面中的市场、规模与动量，不能直接推出BTC/ETH小时策略有效。原始字段定义来自[币安公开数据项目](https://github.com/binance/binance-public-data)。多轮选择偏差参考[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。

未持有点时历史持仓量、盘口、清算流和注意力数据的完整档案，本轮不伪造这些因子。真实历史订单和保证金规则仍未核实，继续沿用既有代理、固定2.5%维持保证金假设。基础单边0.06%手续费＋0.02%不利价格调整，压力0.06%＋0.06%，另计资金费和步长取整。100USDT、单笔0.5%风险、总敞口基准1倍保持不变。2026未读取，没有连接实盘，正式选模资格仍为false。
