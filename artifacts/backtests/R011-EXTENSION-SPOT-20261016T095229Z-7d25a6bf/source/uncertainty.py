"""Conditional paired block-bootstrap diagnostic, not a correction for past tuning."""
import math
import random
from .config import require


def quantile(values,p):
    values=sorted(values)
    x=(len(values)-1)*p
    lo=int(x)
    hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(x-lo)


def paired_cagr_diagnostic(baseline_equity,candidate_equity,block_days=14,resamples=2000,seed=20261015):
    require(len(baseline_equity)==len(candidate_equity) and len(baseline_equity)>=block_days,"Unaligned bootstrap series")
    require(type(block_days) is int and block_days>0 and type(resamples) is int and resamples>=100,"Invalid bootstrap scope")
    n=len(baseline_equity)
    series=[]
    for equity in (baseline_equity,candidate_equity):
        previous=100.0
        logs=[]
        for value in equity:
            require(math.isfinite(value) and value>0,"Nonpositive equity cannot be log-bootstrapped")
            logs.append(math.log(value/previous))
            previous=value
        prefix=[0.0]
        for value in logs+logs:
            prefix.append(prefix[-1]+value)
        series.append(prefix)
    rng=random.Random(seed)
    differences=[]
    for _ in range(resamples):
        totals=[0.0,0.0]
        remaining=n
        while remaining:
            length=min(block_days,remaining)
            start=rng.randrange(n)
            for j,prefix in enumerate(series):
                totals[j]+=prefix[start+length]-prefix[start]
            remaining-=length
        differences.append(math.expm1(365*totals[1]/n)-math.expm1(365*totals[0]/n))
    return {"method":"paired_circular_moving_block_daily_log_returns","block_days":block_days,"resamples":resamples,"seed":seed,
            "cagr_improvement_lower_95":quantile(differences,.025),"cagr_improvement_upper_95":quantile(differences,.975),
            "cagr_improvement_median":quantile(differences,.5),
            "independent_validation":False,"selection_adjusted":False,"probability_of_future_profit":None}
