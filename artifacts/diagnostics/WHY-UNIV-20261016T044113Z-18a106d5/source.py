"""Describe saved account losses; no additional strategy or parameter trials."""
import csv,json,statistics
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timezone

ROOT=Path(r'D:\量化')
RUN=ROOT/'artifacts/backtests/UNIV-20261016T044113Z-18a106d5'

def read(name):
    with (RUN/(name+'.csv')).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))

def trade_summary(rows):
    wins=[float(x['net_pnl']) for x in rows if float(x['net_pnl'])>0]
    losses=[float(x['net_pnl']) for x in rows if float(x['net_pnl'])<0]
    gross=sum(float(x['gross_pnl']) for x in rows)
    fees=sum(float(x['fees']) for x in rows)
    funding=sum(float(x['funding']) for x in rows)
    net=sum(float(x['net_pnl']) for x in rows)
    assert abs(gross-fees+funding-net)<1e-9
    w,l=statistics.mean(wins),-statistics.mean(losses)
    groups=defaultdict(list)
    for x in rows:groups[x['exit_reason']].append(x)
    return {'n':len(rows),'winners':len(wins),'win_rate':len(wins)/len(rows),'average_net_win':w,'average_net_loss':l,
            'descriptive_breakeven_win_rate_at_observed_payoffs':l/(l+w),'gross_after_adverse_fills':gross,
            'fees':fees,'funding':funding,'net':net,
            'exits':{k:{'n':len(v),'net':sum(float(x['net_pnl']) for x in v)} for k,v in groups.items()}}

result={'run_id':RUN.name,'new_strategy_runs':0,'holdout_read':False,'results':{}}
for cost in ['base','stress']:
    ten,two=read('universe10_'+cost+'_trades'),read('control2_'+cost+'_trades')
    end=max(int(x['exit_time_us']) for x in ten)
    rows=trade_summary(ten)
    rows['last_exit_utc']=datetime.fromtimestamp(end/1e6,timezone.utc).isoformat()
    rows['control_at_first_common_hour_after_halt']=next(x for x in read('control2_'+cost+'_hourly_equity') if int(x['time_us'])>=end)
    rows['control_closed_trades_until_halt']=trade_summary([x for x in two if int(x['exit_time_us'])<=end])
    orders={};positions={};last=None;multi=aligned=0
    for line in (RUN/('universe10_'+cost+'_events.jsonl')).read_text(encoding='utf-8').splitlines():
        e=json.loads(line)
        if e['type'] not in ['reserve','fill','exit']:continue
        now=int(e['time_us'])
        if last is not None and len(positions)>=2:
            multi+=now-last
            if len(set(positions.values()))==1:aligned+=now-last
        last=now
        if e['type']=='reserve':orders[e['order_id']]=(e['symbol'],e['side'])
        elif e['type']=='fill':
            s,side=orders[e['order_id']];positions[s]=side
        else:positions.pop(e['symbol'],None)
    assert not positions
    rows['multiple_position_hours']=multi/3_600_000_000
    rows['all_same_direction_share_of_multiple_position_time']=aligned/multi
    key=lambda x:(x['symbol'],x['side'],str(x.get('entry_time_us',x.get('time_us'))))
    tenkeys={key(x) for x in ten}
    missing=[x for x in two if int(x['entry_time_us'])<=end and key(x) not in tenkeys]
    sizing=[json.loads(x) for x in (RUN/('universe10_'+cost+'_sizing.jsonl')).read_text(encoding='utf-8').splitlines()]
    smap={key(x):x for x in sizing}
    refused=[smap[key(x)] for x in missing if key(x) in smap and smap[key(x)]['reserve_error']]
    rows['original_entries_absent_before_halt']={'n':len(missing),'same_time_reserve_refused':len(refused),
        'refused_with_other_positions':sum(bool(x['diagnostic']['open_positions']) for x in refused),
        'original_path_net_sum_not_counterfactual_contribution':sum(float(x['net_pnl']) for x in missing)}
    days=defaultdict(list)
    for x in ten:
        if x['exit_reason']=='stop':days[datetime.fromtimestamp(int(x['exit_time_us'])/1e6,timezone.utc).strftime('%Y-%m-%d')].append(x)
    rows['multiple_coin_stop_days']=sum(len({x['symbol'] for x in v})>=2 for v in days.values())
    rows['stop_trades_on_multiple_coin_stop_days']=sum(len(v) for v in days.values() if len({x['symbol'] for x in v})>=2)
    rows['new_eight_coin_net']=sum(float(x['net_pnl']) for x in ten if x['symbol'] not in ['BTCUSDT','ETHUSDT'])
    result['results'][cost]=rows

a,b=(result['results'][c] for c in ['base','stress'])
lines=['# 十币扩展亏损归因','',
'结论：这次新增币种的价格信号没有提供足够的盈利交易，且多币持仓大多同向；费用进一步拖累。共享预算确实改变原币成交，但不能把全部亏损归因于被挤掉的旧机会。以下仅描述保存的2023年至2024年上半年开发回放，没有新增回测或调参。','',
'| 指标 | 基础成本 | 压力成本 |','|---|---:|---:|',
f'| 总交易／盈利笔数 | {a["n"]}／{a["winners"]} | {b["n"]}／{b["winners"]} |',
f'| 胜率 | {a["win_rate"]:.2%} | {b["win_rate"]:.2%} |',
f'| 平均净盈利单／平均净亏损绝对值USDT | {a["average_net_win"]:.4f}／{a["average_net_loss"]:.4f} | {b["average_net_win"]:.4f}／{b["average_net_loss"]:.4f} |',
f'| 按已观察平均盈亏计算的打平胜率 | {a["descriptive_breakeven_win_rate_at_observed_payoffs"]:.2%} | {b["descriptive_breakeven_win_rate_at_observed_payoffs"]:.2%} |',
f'| 毛盈亏（含不利成交价，未扣显式费用） | {a["gross_after_adverse_fills"]:+.4f} | {b["gross_after_adverse_fills"]:+.4f} |',
f'| 手续费 | {a["fees"]:.4f} | {b["fees"]:.4f} |',
f'| 净资金费 | {a["funding"]:+.4f} | {b["funding"]:+.4f} |',
f'| 最终净盈亏 | {a["net"]:+.4f} | {b["net"]:+.4f} |',
f'| 新增八币账面净盈亏 | {a["new_eight_coin_net"]:+.4f} | {b["new_eight_coin_net"]:+.4f} |',
f'| 多持仓时间中全部同方向的占比 | {a["all_same_direction_share_of_multiple_position_time"]:.2%} | {b["all_same_direction_share_of_multiple_position_time"]:.2%} |','',
'打平胜率是按这批交易的平均净盈利和平均净亏损做的描述性算术，不是独立估计的未来目标；改变入场会同时改变平均盈亏。毛盈亏已包括滑点和取整，不能称为完全无成本回测；固定成交路径加回手续费也不是重新回放的无成本策略。','',
f'基础组{a["exits"]["stop"]["n"]}笔止损合计{a["exits"]["stop"]["net"]:+.4f}USDT；39笔小时退出赚7.8276，9笔72小时退出赚13.7902，盈利不足以覆盖止损。多持仓时几乎总是全多或全空，说明币数增加并未带来方向上的分散。不能将同向持仓占比当作资产收益相关系数，本次没有计算收益协方差或证明市场方向是损失的因果来源。','',
f'基础组{a["multiple_coin_stop_days"]}个UTC日期发生多个不同币止损，覆盖{a["stop_trades_on_multiple_coin_stop_days"]}笔止损；压力组分别为{b["multiple_coin_stop_days"]}天、{b["stop_trades_on_multiple_coin_stop_days"]}笔。这是同日聚集，不意味着全部同一时刻发生。','',
'共享预算：基础组在十币停机前缺少19笔原两币路径的入场，19笔都有同刻预留拒单记录，其中15笔当时有其他持仓；压力组为12笔、其中10笔有其他持仓。这证明成交集合受预算和数量约束影响，但被拒旧交易的原路径净收益总和基础为-0.6308、压力为+0.5401USDT，好坏机会都有。该加总不是重新分配资金后的因果贡献，不能把某一笔盈利直接加回净值，也不能认定更换排序会获得这些收益。','',
f'十币最后退出UTC：基础{a["last_exit_utc"]}，压力{b["last_exit_utc"]}。两组在2023年7月触发峰值回撤20%保护，随后持有现金。最后退出后的首个共同整点，原两币权益分别为{float(a["control_at_first_common_hour_after_halt"]["equity"]):.4f}和{float(b["control_at_first_common_hour_after_halt"]["equity"]):.4f}USDT；同期十币已经为85.4072和85.0723。损失差距在2023年7月已形成；截至2024上半年的对比还包含两币继续交易、十币停机的路径差异。','',
'原规则在BTC/ETH既有开发数据上改善过，但从未证明对所有主流币通用；这次固定十币试验显示直接迁移表现下降，不能仅凭一次试验断言所有新增币都不适用，也不能据此删掉XRP等亏损币再宣称有效。具体是哪种突破结构、持仓排序或市场状态造成损失，仍需独立的机制对照，当前账面归因没有证明这些反事实。','',
'本次窗口较短，币池按当前存续主流币选择，存在幸存者偏差和多轮开发选择偏差；历史订单/维保规则仍使用代理。2026数据未读，原策略结果未改，没有连接实盘。', '',
'来源：artifacts/backtests/'+RUN.name+' 下的事件、逐笔交易、小时权益与仓位诊断。']
destination=ROOT/'artifacts/diagnostics'/('WHY-'+RUN.name)
destination.mkdir(parents=True,exist_ok=False)
(destination/'source.py').write_bytes(Path(__file__).read_bytes())
(destination/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
text='\n'.join(lines)+'\n'
(destination/'扩币亏损归因.md').write_text(text,encoding='utf-8')
(ROOT/'docs/扩币亏损归因.md').write_text(text,encoding='utf-8')
print(json.dumps({'saved':str(destination),'new_strategy_runs':0}))
