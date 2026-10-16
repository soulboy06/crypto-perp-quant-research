"""Report fixed signal experiment with costs, discarded winners and failures."""
from pathlib import Path
import csv,json
from .archive import preserve
from .config import require
from .full_data import sha,relative
from .uncertainty import paired_cagr_diagnostic

def read(path):
    with path.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))

def halves(rows):
    values=[];previous=100.0
    for label,begin,end in [('2023H1','2023-01-01','2023-07-01'),('2023H2','2023-07-01','2024-01-01'),('2024H1','2024-01-01','2024-07-01')]:
        part=[x for x in rows if begin<=x['date']<end]
        require(part,'Missing halfyear')
        final=float(part[-1]['equity']);values.append({'halfyear':label,'return':final/previous-1});previous=final
    return values

def report(root,folder):
    root,folder=Path(root).resolve(),Path(folder)
    s=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
    require(s['status']=='completed' and all(s['ten_coin_control_reproduction'].values()),'Control changed')
    plan=s['plan'];screen=plan['screen'];metrics={x['account']:x for x in s['accounts']}
    impact=read(folder/'baseline_gate_impact.csv')
    comparisons={}
    for cost in ['base','stress']:
        b,n=metrics['control10_'+cost],metrics['extension2atr_'+cost]
        bd,nd=[read(folder/(name+'_'+cost+'_daily_equity.csv')) for name in ['control10','extension2atr']]
        require([x['date'] for x in bd]==[x['date'] for x in nd],'Unpaired dates')
        bootstrap=paired_cagr_diagnostic([float(x['equity']) for x in bd],[float(x['equity']) for x in nd],
            block_days=screen['bootstrap_block_days'],resamples=screen['bootstrap_resamples'],seed=screen['bootstrap_seed'])
        bh,nh=halves(bd),halves(nd)
        improved=sum(a['return']>z['return'] for a,z in zip(nh,bh))
        group=[x for x in impact if x['cost']==cost]
        filtered=[x for x in group if x['gate_passed']=='False']
        winners=[x for x in filtered if float(x['baseline_net_pnl'])>0]
        losers=[x for x in filtered if float(x['baseline_net_pnl'])<0]
        checks={'positive_net':n['net_return']>0,'minimum_trades':n['round_trips']>=screen['minimum_roundtrips_each_cost'],
            'drawdown':n['max_adverse_bound_drawdown']<=float(screen['maximum_drawdown']),
            'no_halts_liquidations_ambiguities':not n['halt_reasons'] and n['liquidation_proxy_events']==0 and n['execution_ambiguities']==0,
            'cagr_improved_at_least_1pp':n['cagr']-b['cagr']>=float(screen['minimum_cagr_improvement_percentage_points'])/100,
            'calmar_non_decreasing':n['calmar'] is not None and n['calmar']>=b['calmar'],
            'minimum_two_improved_halves':improved>=screen['minimum_improved_halfyears_of_three'],
            'halfyear_floor':min(x['return'] for x in nh)>=float(screen['minimum_period_return']),
            'conditional_bootstrap_lower_positive':bootstrap['cagr_improvement_lower_95']>0}
        comparisons[cost]={'checks':checks,'numerical_gate_passed':all(checks.values()),'baseline_halves':bh,'candidate_halves':nh,
            'bootstrap':bootstrap,'baseline_entry_filter_impact':{'baseline_n':len(group),'blocked_n':len(filtered),
                'blocked_profitable_n':len(winners),'blocked_profitable_net':sum(float(x['baseline_net_pnl']) for x in winners),
                'blocked_losing_n':len(losers),'blocked_losing_net':sum(float(x['baseline_net_pnl']) for x in losers),
                'blocked_original_path_net':sum(float(x['baseline_net_pnl']) for x in filtered),
                'not_causal_counterfactual':True}}
    passed=all(x['numerical_gate_passed'] for x in comparisons.values())
    lines=['# 十币突破信号质量实验 SIG-001-v1','',
        '只增加一个入场条件：多头(close-SMA20)/ATR14≤2，空头(SMA20-close)/ATR14≤2；SMA20和ATR14均来自当前已完整收盘小时，SMA包含当前收盘，ATR沿用原Wilder更新。原48小时突破、EMA200、完整四小时方向与ER24≥0.30继续生效，不做参数搜索。当前突破已在48小时收盘极值之外，因此不额外加入均价距离下限。','',
        f'运行{s["run_id"]}，耗时{s["runtime_seconds"]:.1f}秒；新增一项假设×两种成本，另两组十币复现对照的逐笔交易和每日权益与原UNIV完全一致。', '',
        '**区间仍为2023-01-01至2024-07-01（不含结束点），另有2022年11—12月热身。不是完整三年回测，也不是独立样本外验证。十币名单、共享100USDT、单笔0.5%、总止损风险1%、基准总敞口1倍、最高10倍及全部退出、成本、保护不变。没有接入实盘，2026历史继续锁定。**','',
        ('两种成本均通过事先固定的数值研究门槛，仍没有正式选模资格。' if passed else '未同时通过两种成本的预设改进门槛，不替换默认研究策略。'), '',
        '| 策略 | 成本 | 本窗口折算年化 | 本窗口累计 | 压力路径最大回撤 | 期末USDT | 交易数 | 胜率 |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for name in ['control10_base','control10_stress','extension2atr_base','extension2atr_stress']:
        m=metrics[name]
        lines.append(f'| {"原十币" if name.startswith("control10") else "限制追价距离"} | {name.rsplit("_",1)[1]} | {m["cagr"]:+.2%} | {m["net_return"]:+.2%} | {m["max_adverse_bound_drawdown"]:.2%} | {float(m["ending_equity"]):.4f} | {m["round_trips"]} | {m["win_rate"]:.2%} |')
    lines+=['','回撤是预设分钟不利标记价路径的观测值，不保证覆盖所有真实路径。不同成本会改变仓位和资金路径，不可当作完全相同交易的费用差。触发永久保护后不再开仓，剩余日期现金零收益不等于持续交易的稳定性。','',
        '## 预设门槛与分期','',
        '两种成本都要求净收益为正、至少30笔、年化改善≥1个百分点、回撤≤20%、没有停机/代理强平/执行歧义、Calmar不降、三个半年至少两个改善且每半年≥-10%，以及14天循环区块配对重采样2000次改善区间下限>0。固定种子20261016。重采样区间仅是条件开发诊断，未修正多轮选择，不能当作未来盈利概率。','']
    for cost,x in comparisons.items():
        m=metrics['extension2atr_'+cost];ci=x['bootstrap']
        lines.append(f'- {cost}：未通过项目{[k for k,v in x["checks"].items() if not v]}；改善区间[{ci["cagr_improvement_lower_95"]*100:+.2f}, {ci["cagr_improvement_upper_95"]*100:+.2f}]个百分点；停机原因{m["halt_reasons"]}，代理强平{m["liquidation_proxy_events"]}，歧义{m["execution_ambiguities"]}。')
    lines+=['','| 半年 | 原十币基础 | 新版基础 | 原十币压力 | 新版压力 |','|---|---:|---:|---:|---:|']
    for j in range(3):
        values=[comparisons[c][k][j]['return'] for c,k in [('base','baseline_halves'),('base','candidate_halves'),('stress','baseline_halves'),('stress','candidate_halves')]]
        lines.append('| '+comparisons['base']['baseline_halves'][j]['halfyear']+' | '+' | '.join(f'{v:+.2%}' for v in values)+' |')
    lines+=['','## 过滤了哪些旧成交','',
        '| 成本 | 旧交易数 | 被过滤数 | 被过滤旧盈利单数／账面盈利USDT | 被过滤旧亏损单数／账面亏损USDT |',
        '|---|---:|---:|---:|---:|']
    for cost,x in comparisons.items():
        d=x['baseline_entry_filter_impact']
        lines.append(f'| {cost} | {d["baseline_n"]} | {d["blocked_n"]} | {d["blocked_profitable_n"]}／{d["blocked_profitable_net"]:+.4f} | {d["blocked_losing_n"]}／{d["blocked_losing_net"]:+.4f} |')
    lines+=['','这里用每个旧入场时点的已闭合特征独立判断门槛，覆盖全部保存旧成交。旧账户在2023年7月停机，因此只覆盖停机前的旧成交；之后候选账户可能产生的新机会没有这个旧路径对照。旧净盈亏在决策之后仅用于离线诊断，没有输入候选决策。被过滤交易的原路径盈亏不能直接加回新账户，也不证明过滤器有同等因果收益；共享预算、持仓占用和保护会改变后续路径。','',
        '## 分币表现及限制','','| 币种 | 新版基础交易／净盈亏USDT | 新版压力交易／净盈亏USDT |','|---|---:|---:|']
    for symbol in s['symbols']:
        cells=[]
        for cost in ['base','stress']:
            group=[x for x in read(folder/('extension2atr_'+cost+'_trades.csv')) if x['symbol']==symbol]
            cells.append(f'{len(group)}／{sum(float(x["net_pnl"]) for x in group):+.4f}')
        lines.append('| '+symbol+' | '+' | '.join(cells)+' |')
    lines+=['','所有币、所有账户保留，不按成绩删币或改阈值。原十币名单按当前存续主流币固定，存在幸存者偏差；历史过滤器及维保档位仍是代理。价格策略不使用XRP已标记异常的成交量字段。原始数据、旧基准和金融核心均未改变。','',
        '2ATR门槛只是一次事前固定的机制假设，没有外部研究证明这个小时级阈值在加密永续有效。多次在相同开发历史上试规则会增加选择偏差，参考[Bailey等关于回测过拟合的研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。本次即使有改善也只能作后续研究依据。']
    payload={'run_id':s['run_id'],'summary_sha256':sha((folder/'summary.json').read_bytes()),'comparisons':comparisons,
             'numerical_gate_passed':passed,'formal_selection_eligible':False,'holdout_read':False}
    text='\n'.join(lines)+'\n'
    preserve(folder/'comparison.json',json.dumps(payload,ensure_ascii=False,indent=2).encode())
    preserve(folder/'comparison.md',text.encode())
    (root/'docs/十币信号质量优化结果.md').write_text(text,encoding='utf-8')
    latest={'run_id':s['run_id'],'research_design_id':s['research_design_id'],'scope':'common_complete_2023_to_2024H1_development_proxy_rules',
        'summary':relative(root,folder/'summary.json'),'report':relative(root,folder/'comparison.md'),'summary_sha256':payload['summary_sha256'],
        'research_baseline_retained':s['baseline_run'],'retained_development_candidates':['extension2atr'] if passed else [],
        'holdout_read':False,'formal_selection_eligible':False,'full_2023_2025_result':False,'expanded_symbols':s['symbols']}
    (root/'artifacts/backtests/latest.json').write_text(json.dumps(latest,ensure_ascii=False,indent=2),encoding='utf-8')
    return folder/'comparison.md',payload
