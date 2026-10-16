"""Report every fixed-universe account; do not select coins after seeing returns."""
from collections import Counter,defaultdict
from pathlib import Path
import csv,json
from .archive import preserve
from .config import require
from .full_data import sha,relative

def report(root,folder=None):
    root=Path(root).resolve()
    if folder is None:
        pointer=json.loads((root/'artifacts/backtests/universe_latest.json').read_text(encoding='utf-8'))
        folder=root/'artifacts/backtests'/pointer['run_id']
        require(sha((folder/'summary.json').read_bytes())==pointer['summary_sha256'],'Summary changed')
    folder=Path(folder)
    summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    require(summary['status']=='completed' and all(summary['original_two_coin_reproduction'].values()),'Control not reproduced')
    data=json.loads((root/summary['data_manifest']).read_text(encoding='utf-8'))
    rules=json.loads((root/data['rules_path']).read_text(encoding='utf-8'))
    accounts={x['account']:x for x in summary['accounts']}
    lines=['# 十币主流币池扩展结果 UNIV-001-v1','',
      '研究币池已从BTC、ETH扩至10币：BTC、ETH、BNB、SOL、XRP、DOGE、ADA、LINK、AVAX、LTC。各币共用100USDT模拟本金，保持原R004效率过滤突破策略；没有为每个币另配100USDT。', '',
      f'运行：{summary["run_id"]}，{summary["runtime_seconds"]:.1f}秒。新增2个候选×成本配置，另有2个同窗口两币对照；在末尾强制平仓前，两币逐笔交易与每日权益前缀均与保存的R004一致。结束时按原执行模型平仓，末尾费用与原三年持仓路径可能不同。', '',
      '**本次可比窗口为2023-01-01至2024-07-01（不含结束时点），即2023全年和2024上半年；不是完整2023—2025三年结果。表中年化按这段约18个月的净值折算，累计收益也是这段期间的收益。** 原计划准备三年数据，但资金费公开接口对部分2024年7月及以后分区返回403，多个币在2024-08-12还有两分钟标记价缺口，官方月度/日度/分钟REST均未提供；没有填造这些价格。按照运行前固定的UNIV-COVERAGE-001-v1，先使用共同完整月份；这一日期选择基于数据可用性，在十币策略成绩计算之前确定。', '',
      '新增币种按当前主流程度固定，全部早于2022年11月研究起点上线，属于事后选择的存续币列表；不是当时可知的市值或流动性排名。另有2022年11—12月热身；2026年历史没有读取。', '',
      '## 扣费后的共享账户结果','',
      '| 账户 | 成本 | 本窗口折算年化 | 本窗口累计 | 压力路径最大回撤 | 期末USDT | 完成交易 |',
      '|---|---|---:|---:|---:|---:|---:|']
    details={}
    for name in ['control2_base','control2_stress','universe10_base','universe10_stress']:
        m=accounts[name]
        lines.append(f'| {"原两币" if name.startswith("control2") else "十币组合"} | {name.rsplit("_",1)[1]} | {m["cagr"]:+.2%} | {m["net_return"]:+.2%} | {m["max_adverse_bound_drawdown"]:.2%} | {float(m["ending_equity"]):.4f} | {m["round_trips"]} |')
        with (folder/(name+'_trades.csv')).open(encoding='utf-8-sig') as f:trades=list(csv.DictReader(f))
        grouped=defaultdict(list)
        for x in trades:grouped[x['symbol']].append(x)
        breakdown={s:{'trades':len(grouped[s]),'gross_pnl':sum(float(x['gross_pnl']) for x in grouped[s]),
                       'fees':sum(float(x['fees']) for x in grouped[s]),'funding':sum(float(x['funding']) for x in grouped[s]),
                       'net_pnl':sum(float(x['net_pnl']) for x in grouped[s])} for s in summary['plan']['symbols']}
        require(abs(sum(x['net_pnl'] for x in breakdown.values())-(float(m['ending_equity'])-100))<1e-7,'Cash attribution mismatch')
        open_symbols=set();max_positions=0
        for line in (folder/(name+'_events.jsonl')).read_text(encoding='utf-8').splitlines():
            e=json.loads(line)
            if e['type']=='fill':
                # This execution path uses full fills; recover order's symbol from order_id.
                s=e['order_id'].split('-')[0];open_symbols.add(s)
            elif e['type']=='exit':open_symbols.discard(e['symbol'])
            max_positions=max(max_positions,len(open_symbols))
        details[name]={'by_symbol':breakdown,'maximum_simultaneous_positions':max_positions,
                       'sizing_diagnostics':m['sizing_diagnostics'],'execution_ambiguities':m['execution_ambiguities'],
                       'liquidation_proxy_events':m['liquidation_proxy_events'],'halt_reasons':m['halt_reasons']}
    lines+=['','最大回撤为预设分钟不利标记价路径的观测值，不能保证覆盖所有真实路径或未来亏损。不同成本会改变成交、仓位和资金路径。','']
    for cost in ['base','stress']:
        b,n=accounts['control2_'+cost],accounts['universe10_'+cost]
        lines.append(f'- {cost}：扩币后年化变化{(n["cagr"]-b["cagr"])*100:+.2f}个百分点，回撤变化{(n["max_adverse_bound_drawdown"]-b["max_adverse_bound_drawdown"])*100:+.2f}个百分点；代理强平{n["liquidation_proxy_events"]}次，执行歧义{n["execution_ambiguities"]}次，停机原因{n["halt_reasons"]}。')
    lines+=['','## 分期结果与风控状态','','| 账户 | 2023全年 | 2024上半年 | 最大同时持仓数 |','|---|---:|---:|---:|']
    for name,m in accounts.items():
        rates=[f'{m["annual"][str(y)]["return"]:+.2%}' for y in [2023,2024]]
        lines.append('| '+name+' | '+' | '.join(rates)+f' | {details[name]["maximum_simultaneous_positions"]} |')
    lines+=['','永久回撤保护触发后保持现金，后续零收益不能当作持续交易的稳定性；若触发保护，应按这一事实解释全年及全期年化。保护值20%不是严格损失上限，跳价、费用可能使回撤超过它。','',
      '## 分币种账面贡献','','| 币种 | 基础交易数 | 基础净盈亏USDT | 压力交易数 | 压力净盈亏USDT |','|---|---:|---:|---:|---:|']
    for s in summary['plan']['symbols']:
        a,b=(details['universe10_'+c]['by_symbol'][s] for c in ['base','stress'])
        lines.append(f'| {s} | {a["trades"]} | {a["net_pnl"]:+.4f} | {b["trades"]} | {b["net_pnl"]:+.4f} |')
    lines+=['','分币贡献来自同一个共享账户，不是各币独立年化；加入币种会改变原币的交易机会，不能把单币利润简单相加构造新账户，也不会按本次结果删掉亏损币重跑。','',
      '## 最低订单与费用约束','','| 币种 | 保存快照最小名义USDT | 最小数量 | 数量步长 |','|---|---:|---:|---:|']
    for s in summary['plan']['symbols']:
        filters={x['filterType']:x for x in rules['symbols'][s]['filters']}
        lot=filters['MARKET_LOT_SIZE']
        lines.append(f'| {s} | {filters["MIN_NOTIONAL"]["notional"]} | {lot["minQty"]} | {lot["stepSize"]} |')
    lines+=['','BTC/ETH沿用原规则快照以保持对照；新币使用此次公开快照。历史逐时过滤器尚未核实，以上不是2023—2025真实规则证明。名义金额最低5USDT不代表总能下单，还须满足最小数量、步长和风险预算，例如AVAX当前保存步长为1个币。','',
      '基础单边手续费0.06%＋不利价格调整0.02%，压力0.06%＋0.06%，另计实际历史资金费、价格步长和数量取整。沿用固定2.5%维持保证金压力假设、逐仓、关闭自动追加、同币先平后反向，不是精确历史保证金档位复现。','',
      '## 数据与实现','','完整三年计划需要新增8币×38个月×3类共912个分区；本次实际使用新增8币×20个月×3类共480个分区，复用两币相同20个月的120个已验收分区。已下载但不在共同窗口内的数据全部保留，不混入本次成绩。新数据核对官方校验和、时间和OHLC，资金费逐条对照先前已保存的官方REST。缺失分钟仅从官方观测来源恢复，不填造价格。SOL等资金费间隔变化保留原始时间，接受相邻已报告间隔支持的实际转换并核对REST时点，转换记录在数据清单中。','',
      'XRP 2023年11月一条记录的主动买入量大于总量，官方REST有同样的异常。原始字段保留不修造，OHLC与相邻REST观测一致，标准策略输入只包含价格与时间；清单标记volume_fields_valid=false和volume_features_permitted=false。这批数据可用于本次价格策略，不得未经额外处理用于成交量/主动买卖量因子。','',
      '策略仍为48小时突破、小时EMA200、最近完整四小时EMA20/50方向、24小时效率≥0.30；确认后延迟60秒，原2ATR止损、小时EMA50退出与72小时期限。单笔0.5%、账户已开和待成交总止损风险1%、基准总敞口1倍、最高10倍、20%回撤保护和50USDT本金停止线均未修改。','',
      '同时出现信号时按固定BTC、ETH、BNB、SOL、XRP、DOGE、ADA、LINK、AVAX、LTC顺序尝试，不按回测盈利或当期收益排名，存在先到信号占用共享风险预算的优先级影响。多币相关性意味着币种增加不保证风险下降。','',
      '五项关键检查覆盖：仅币池变动、共享资金和总风险、新币完整小时与60秒延迟、两币双成本一致性、资金费间隔转换。再复核同窗口原两币逐笔交易和每日权益前缀，末尾强制平仓例外单独处理。原账本、风险、信号和旧配置代码未修改；新UniverseLedger仅扩展受支持的初始币池和规则映射，全部金融事件沿用原ReplayLedger/Ledger。','',
      '本轮仅为共同完整区间的扩币开发比较。当前存续币选择造成幸存者偏差，原开发时期已多轮观察，窗口较短，未取得独立验证资格，也未连接实盘。扩大币池成功接入与策略收益提升是不同结论；所有账户结果均保留。完整三年十币结果尚缺上述数据证据，不因已有成绩好坏改变日期或删除币种。','',
      f'数据清单：{summary["data_manifest"]}；结果目录：{relative(root,folder)}。']
    text='\n'.join(lines)+'\n'
    preserve(folder/'comparison.md',text.encode())
    preserve(folder/'comparison.json',json.dumps({'run_id':summary['run_id'],'summary_sha256':sha((folder/'summary.json').read_bytes()),'details':details,
        'formal_selection_eligible':False,'holdout_read':False},ensure_ascii=False,indent=2).encode())
    (root/'docs/主流币扩展结果.md').write_text(text,encoding='utf-8')
    return folder/'comparison.md',details
