"""Select compatible daily paths by an integer program (PuLP/CBC).

Original integration, inspired by spatial optimization and independent constraint
validation in the cited research. Exact only over the retained path pool, not all POIs.
"""
import time
from dataclasses import replace
from decimal import Decimal
from .itinerary_optimizer import State,extend,prepare_search,cents,rank


def daily_paths(candidates,p,budget,known,errors,closed,seed,limit=200):
    estimates,days=prepare_search(candidates,p);cap=2 if p['pace']=='relaxed' else 3
    groups=[]
    for day_index,(day,start,end,prepared) in enumerate(days):
        active=[State(clock=start,lunch=start>=13*60)];best={0:State()}
        rejected=dict(time=0,budget=0,travel=0,closed=0)
        for _ in range(cap):
            expanded=[]
            for state in active:
                for i,row in enumerate(prepared):
                    s=extend(state,i,row,p,day,end,cents(budget),known,errors,estimates,closed,rejected)
                    if s:
                        expanded.append(s)
                        old=best.get(s.used)
                        if old is None or rank(s)>rank(old):best[s.used]=s
            active=expanded
        pool=list(best.values());picked={0:best[0]}
        # Preserve incumbent day paths and all singletons. This cannot discard the incumbent solution.
        for s in pool:
            if s.used.bit_count()==1:picked[s.used]=s
        seed_ids={r['poi_id'] for r in seed.days[day_index]} if day_index<len(seed.days) else set()
        seed_mask=sum(1<<i for i,r in enumerate(candidates) if r['poi_id'] in seed_ids)
        if seed_mask in best:picked[seed_mask]=best[seed_mask]
        orders=[sorted(pool,key=lambda s:rank(s),reverse=True),
                sorted(pool,key=lambda s:(s.cost,-s.utility)),
                sorted(pool,key=lambda s:(-s.reference_spend,-s.utility)),
                sorted(pool,key=lambda s:(-s.utility/max(1,s.cost),s.cost))]
        # Round-robin avoids filling the pool with one high-score or high-price family.
        for offset in range(max(map(len,orders),default=0)):
            for order in orders:
                if offset<len(order):picked.setdefault(order[offset].used,order[offset])
                if len(picked)>=max(limit,len(candidates)+2):break
            if len(picked)>=max(limit,len(candidates)+2):break
        groups.append(list(picked.values()))
    return groups


def solve_paths(candidates,p,budget,known,errors,closed,seed,seconds=1.5,path_limit=200):
    started=time.monotonic()
    report={'engine':'PuLP/CBC日路径整数规划','status':'fallback','path_pool':0,'seconds':0,
            'scope':'仅对保留的日路径池求解，不是全国/全部候选全局最优'}
    try:
        import pulp
        solver=pulp.PULP_CBC_CMD(msg=False,timeLimit=seconds,threads=None,gapRel=0,warmStart=True)
        if not solver.available():raise RuntimeError('CBC unavailable')
    except (ImportError,RuntimeError,OSError):
        report.update(status='solver_unavailable',seconds=round(time.monotonic()-started,3))
        return seed,report
    groups=daily_paths(candidates,p,budget,known,errors,closed,seed,path_limit)
    report['path_pool']=sum(map(len,groups))
    model=pulp.LpProblem('daily_itinerary',pulp.LpMaximize)
    variables={(d,j):pulp.LpVariable(f'p_{d}_{j}',cat='Binary') for d,g in enumerate(groups) for j in range(len(g))}
    for d,g in enumerate(groups):model+=pulp.lpSum(variables[d,j] for j in range(len(g)))==1
    for i in range(len(candidates)):
        model+=pulp.lpSum(variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g) if s.used&(1<<i))<=1
    cost=pulp.lpSum(s.cost*variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g))
    model+=cost<=cents(budget)
    plays=pulp.lpSum(bool(s.current)*variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g))
    stops=pulp.lpSum(len(s.current)*variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g))
    day_target=min(p['days'],p.get('_minimum_play_days',0));stop_target=p.get('_minimum_sights',0)
    target_days=pulp.LpVariable('target_days',0,day_target,cat='Integer');model+=target_days<=plays
    target_stops=pulp.LpVariable('target_stops',0,stop_target,cat='Integer');model+=target_stops<=stops
    themes=[]
    for i,_ in enumerate(p.get('keywords',[])):
        v=pulp.LpVariable('theme_'+str(i),cat='Binary')
        model+=v<=pulp.lpSum(variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g) if s.themes&(1<<i))
        themes.append(v)
    quality=pulp.lpSum(round((s.utility-18*s.themes.bit_count())*1000)*variables[d,j]
                       for d,g in enumerate(groups) for j,s in enumerate(g))+18000*pulp.lpSum(themes)
    upper=sum(max((round(max(0,s.utility)*1000) for s in g),default=0) for g in groups)+18000*len(themes)
    stop_weight=upper+1;day_weight=(stop_target+1)*stop_weight+upper+1
    objective=day_weight*target_days+stop_weight*target_stops+quality
    model.setObjective(objective)
    # Give CBC an incumbent using the already feasible daily paths.
    for d,g in enumerate(groups):
        ids={r['poi_id'] for r in seed.days[d]} if d<len(seed.days) else set()
        mask=sum(1<<i for i,r in enumerate(candidates) if r['poi_id'] in ids)
        for j,s in enumerate(g):variables[d,j].setInitialValue(int(s.used==mask))
    def extract():
        chosen=[];used=0;cost_total=0
        for d,g in enumerate(groups):
            # Reject a fractional relaxation, incomplete result or solver failure.
            values=[variables[d,j].value() for j in range(len(g))]
            if any(v is None or min(abs(v),abs(v-1))>1e-5 for v in values):return None
            selected=[s for j,s in enumerate(g) if values[j]>.5]
            if len(selected)!=1:return None
            s=selected[0]
            if used&s.used:return None
            used|=s.used;cost_total+=s.cost;chosen.append(s.current)
        if cost_total>cents(budget):return None
        # Replay with shared constraints and cross-day theme state, independent of MILP values.
        estimates,days=prepare_search(candidates,p);indexes={r['poi_id']:i for i,r in enumerate(candidates)}
        s=State();rejected=dict(time=0,budget=0,travel=0,closed=0)
        for path,(day,start,end,prepared) in zip(chosen,days):
            s=replace(s,current=(),clock=start,lunch=start>=13*60)
            for row in path:
                i=indexes[row['poi_id']]
                s=extend(s,i,prepared[i],p,day,end,cents(budget),known,errors,estimates,closed,rejected)
                if s is None:return None
            s=replace(s,days=s.days+(s.current,),current=(),clock=0,lunch=False)
        return s
    try:
        model.solve(solver)
        state=extract()
        if state is None:report['status']='no_integer_solution';return seed,report
        incumbent=state
        proven=model.sol_status==pulp.LpSolutionOptimal
        report['quality_optimal_in_pool']=proven
        # Budget is secondary to the achieved quality/coverage, not a reason to lower them by 3%.
        score=pulp.value(objective)
        if p.get('budget_strategy')=='experience' and score is not None:
            model+=objective>=round(score)-.01
            value=pulp.lpSum(s.reference_spend*variables[d,j] for d,g in enumerate(groups) for j,s in enumerate(g))
            # Reward source-based references only. Unknown reserves never create upgrade value.
            model.setObjective(value-cost/(max(1,cents(budget))+1))
            model.solve(solver)
            state=extract() or incumbent
        minimums=(p.get('_minimum_sights',0),p.get('_minimum_play_days',0))
        if rank(state,*minimums)[:3]<rank(seed,*minimums)[:3]:state=seed;report['status']='retained_better_incumbent'
        else:report['status']='integer_feasible'
        return state,report
    except (pulp.PulpSolverError,OSError,ValueError):
        report['status']='solver_error';return seed,report
    finally:report['seconds']=round(time.monotonic()-started,3)
