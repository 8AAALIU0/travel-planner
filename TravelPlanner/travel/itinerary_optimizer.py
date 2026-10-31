"""多日、预算及时间窗约束的有界束搜索。比较多个组合，不声称全局最优。"""
from dataclasses import dataclass,replace
from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .amap_client import AmapError
from .planner_api import cache_key,read_cache_many,write_cache
from .planner_service import day_window,monday_closed,opening_window,estimate_leg,time_text,route_link


@dataclass(frozen=True)
class State:
    used:int=0
    themes:int=0
    cost:int=0
    utility:float=0
    reference_spend:int=0
    travel:int=0
    days:tuple=()
    current:tuple=()
    clock:int=0
    lunch:bool=False


def cents(amount):
    return int((Decimal(str(amount))*100).quantize(Decimal('1')))


def ticket_for_day(source,p,day):
    row=dict(source)
    if p.get('ticket_policy'):
        from .ticket_budgeting import apply
        return apply(row,p,day)
    ref=next((r for r in p.get('ticket_refs',[]) if r.get('name')==row['name'] and r.get('date')==day.isoformat()),None)
    if ref:
        stamp=parse_datetime(ref.get('queried_at',''))
        if stamp and timezone.is_aware(stamp) and 0<=(timezone.now()-stamp).total_seconds()<=86400:
            try:
                amount=Decimal(ref['unit'])
                if amount.is_finite() and amount>=0:
                    row.update(ticket_per_person=str(max(amount,Decimal(row['ticket_per_person']))),price_reference=False,tuniu_reference=True,ticket_source_url='',
                        ticket_type=('接口成人票起价参考：' if ref.get('auto') else '用户选定票种参考：')+ref['label']+'；起价日期 '+ref.get('priceday','未知')+'。仍为预算预留，非当天实价。')
            except (ValueError,ArithmeticError,KeyError):pass
    return row


def edge_key(a,b):return (a['poi_id'],b['poi_id'])


def rank(s,min_stops=0,min_days=0):
    count=sum(len(d) for d in s.days)+len(s.current)
    play_days=sum(bool(d) for d in s.days)+bool(s.current)
    return (min(play_days,min_days),min(count,min_stops),s.utility,count,-s.travel,-s.cost)


def select_candidates(candidates,limit=32):
    """Keep quality leaders plus theme, affordability and nearby-cluster representatives."""
    if len(candidates)<=limit:return list(candidates)
    from .planner_service import distance
    picked=[];seen=set()
    def add(row):
        if row['poi_id'] not in seen and len(picked)<limit:
            picked.append(row);seen.add(row['poi_id'])
    for row in candidates[:max(1,limit//2)]:add(row)
    for word in dict.fromkeys(w for r in candidates for w in r.get('keyword_matches',[])):
        group=[r for r in candidates if word in r.get('keyword_matches',[])]
        if group:add(min(group,key=lambda r:(Decimal(r['ticket_per_person']),-r.get('score',0))))
    for row in sorted(candidates,key=lambda r:(Decimal(r['ticket_per_person']),-r.get('score',0)))[:max(2,limit//4)]:add(row)
    # A high-rated remote POI must not crowd all members of a practical local cluster out.
    for anchor in candidates[:4]:
        for row in sorted(candidates,key=lambda r:distance(anchor,r))[:3]:add(row)
    for row in candidates:add(row)
    return picked


def choose_state(states,p,min_stops=0,min_days=0):
    """Epsilon constraint: at best found quality, prefer sourced ticket value.

    Budget use must never lower the achieved quality score.
    Unknown estimates never earn budget-utilization reward.
    """
    best=max(states,key=lambda s:rank(s,min_stops,min_days))
    if p.get('budget_strategy')!='experience':return best
    target=rank(best,min_stops,min_days)[:2]
    count=sum(map(len,best.days))+len(best.current)
    feasible=[s for s in states if rank(s,min_stops,min_days)[:2]==target
              and sum(map(len,s.days))+len(s.current)>=count
              and s.utility>=best.utility-1e-8]
    return max(feasible,key=lambda s:(s.reference_spend,s.utility,-s.travel,-s.cost))


def prune(states,width,min_stops=0,min_days=0):
    best={}
    for s in states:
        last=s.current[-1]['poi_id'] if s.current else ''
        # Include the complete ordered past: the same set on different dates can have different prices/windows.
        key=(s.used,last,tuple(tuple(r['poi_id'] for r in d) for d in s.days),len(s.current),s.clock,s.cost,s.lunch)
        if key not in best or rank(s,min_stops,min_days)>rank(best[key],min_stops,min_days):best[key]=s
    ordered=sorted(best.values(),key=lambda s:rank(s,min_stops,min_days),reverse=True)
    if len(ordered)<=width:return ordered
    if width<=1:return ordered[:1]
    # Reserve one quarter of the beam for varied cost/theme states; avoid exhausting the budget on day one.
    kept=ordered[:max(1,width*3//4)]
    identities={id(s) for s in kept};buckets=set()
    scale=max(1,max(s.cost for s in ordered)//8+1)
    for s in ordered:
        bucket=(s.cost//scale,s.themes,rank(s,min_stops,min_days)[:2])
        if bucket not in buckets and id(s) not in identities:
            kept.append(s);identities.add(id(s));buckets.add(bucket)
            if len(kept)==width:return kept
    for s in ordered:
        if id(s) not in identities:kept.append(s);identities.add(id(s))
        if len(kept)==width:break
    return kept


def prepare_search(candidates,p):
    estimates={(a['poi_id'],b['poi_id']):estimate_leg(a,b,p['mode']) for a in candidates for b in candidates if a['poi_id']!=b['poi_id']}
    days=[]
    for index in range(p['days']):
        day=p['start_date']+timedelta(days=index)
        start,end=day_window(p,index)
        days.append((day,start,end,[ticket_for_day(r,p,day) for r in candidates]))
    return estimates,days


def extend(s,i,row,p,day,end,limit,known,errors,estimates,closed,rejected):
    if s.used & (1<<i):return None
    if monday_closed(row,day):rejected['closed']+=1;return None
    amount=cents(Decimal(row['ticket_per_person'])*p['people'])
    if s.cost+amount>limit:rejected['budget']+=1;return None
    leg=None
    if s.current:
        pair=edge_key(s.current[-1],row)
        if closed and pair not in known and pair not in errors:rejected['travel']+=1;return None
        leg=dict(known.get(pair) or estimates[pair])
        if pair in errors:leg['error']=errors[pair]
        if leg['minutes']>(60 if p['mode']=='walking' else 90):rejected['travel']+=1;return None
    begin=s.clock+(leg['minutes'] if leg else 0)
    window=opening_window(row)
    if window:begin=max(begin,window[0])
    lunch=s.lunch
    lunch_before=bool(s.current and not lunch and begin<13*60 and begin+row['visit_minutes']>12*60)
    if lunch_before:begin+=45;lunch=True
    finish=begin+row['visit_minutes']
    if finish>end or (window and finish>window[1]):rejected['time']+=1;return None
    travel=leg['minutes'] if leg else 0
    gain=100+5*min(30,max(0,float(row.get('score',0))))-.55*travel-.025*max(0,begin-s.clock-travel)
    gain+=8 if not s.current else 0
    gain+=2*(p['days']-(day-p['start_date']).days)
    theme_bits=sum(1<<j for j,w in enumerate(p.get('keywords',[])) if w in row.get('keyword_matches',[]))
    gain+=18*(theme_bits & ~s.themes).bit_count()
    ref=row.get('ticket_estimate',{})
    # Count only sourced reference amounts, never uncertainty margins as an experience upgrade.
    sourced=ref.get('kind') in ('official','record','platform') or row.get('price_reference') or row.get('tuniu_reference')
    value=cents(Decimal(ref.get('low',row['ticket_per_person']))*p['people']) if sourced else 0
    stop=dict(row,begin=time_text(begin),end=time_text(finish),leg=leg,group_ticket=str(Decimal(amount)/100),
              hours_checked=bool(window),lunch_before=lunch_before,
              map_link=route_link(s.current[-1],row,p['mode']) if s.current else '')
    return replace(s,used=s.used|(1<<i),themes=s.themes|theme_bits,cost=s.cost+amount,
                   reference_spend=s.reference_spend+min(value,amount),utility=s.utility+gain,
                   travel=s.travel+travel,current=s.current+(stop,),clock=finish,lunch=lunch)


def search(candidates,p,ticket_budget,known=None,errors=None,closed=False,width=48,context=None):
    known=known or {};errors=errors or {}
    cap=2 if p['pace']=='relaxed' else 3;limit=cents(ticket_budget)
    estimates,days=context or prepare_search(candidates,p)
    min_stops=p.get('_minimum_sights',0);min_days=p.get('_minimum_play_days',0)
    states=[State()];metrics=[]
    for day,start,end,prepared in days:
        active=[replace(s,current=(),clock=start,lunch=start>=13*60) for s in states];completed=[]
        rejected={'time':0,'budget':0,'travel':0,'closed':0}
        for slot in range(cap):
            completed.extend(active);expanded=[]
            for s in active:
                for i,row in enumerate(prepared):
                    next_state=extend(s,i,row,p,day,end,limit,known,errors,estimates,closed,rejected)
                    if next_state:expanded.append(next_state)
            if not expanded:active=[];break
            active=prune(expanded,width,min_stops,min_days)
        completed.extend(active)
        states=prune([replace(s,days=s.days+(s.current,),current=(),clock=0,lunch=False) for s in completed],width,min_stops,min_days)
        metrics.append(rejected)
    return choose_state(states,p,min_stops,min_days),metrics


def improve(state,candidates,p,ticket_budget,known,errors,closed=False,max_evaluations=900):
    """Bounded insert/replace/relocate/swap local search, no network or DB calls.

    Inspired by TOPTW local-search neighborhoods; this is not a reproduction of the cited ILS solver.
    """
    estimates,days=prepare_search(candidates,p);limit=cents(ticket_budget)
    cap=2 if p['pace']=='relaxed' else 3
    index={r['poi_id']:i for i,r in enumerate(candidates)}
    goals=(p.get('_minimum_sights',0),p.get('_minimum_play_days',0))
    attempts=0;seen=set();archive=[state]
    def replay(paths):
        nonlocal attempts
        token=tuple(tuple(path) for path in paths)
        if token in seen or attempts>=max_evaluations:return None
        seen.add(token);attempts+=1;s=State();rejected=dict(time=0,budget=0,travel=0,closed=0)
        for path,(day,start,end,prepared) in zip(paths,days):
            if len(path)>cap:return None
            s=replace(s,current=(),clock=start,lunch=start>=13*60)
            for i in path:
                s=extend(s,i,prepared[i],p,day,end,limit,known,errors,estimates,closed,rejected)
                if s is None:return None
            s=replace(s,days=s.days+(s.current,),current=(),clock=0,lunch=False)
        return s
    for _ in range(3):
        paths=[[index[r['poi_id']] for r in path] for path in state.days]
        used={i for path in paths for i in path};unused=[i for i in range(len(candidates)) if i not in used]
        def neighbors():
            for d,path in enumerate(paths):
                for i in unused:
                    if len(path)<cap:
                        for pos in range(len(path)+1):
                            q=[x[:] for x in paths];q[d].insert(pos,i);yield q
                    for pos in range(len(path)):
                        q=[x[:] for x in paths];q[d][pos]=i;yield q
            positions=[(d,j) for d,path in enumerate(paths) for j in range(len(path))]
            for a,(d,j) in enumerate(positions):
                for e,k in positions[a+1:]:
                    q=[x[:] for x in paths];q[d][j],q[e][k]=q[e][k],q[d][j];yield q
                for e,path in enumerate(paths):
                    if e!=d and len(path)>=cap:continue
                    for k in range(len(path)+1):
                        q=[x[:] for x in paths];value=q[d].pop(j);q[e].insert(k,value);yield q
        for paths2 in neighbors():
            if attempts>=max_evaluations:break
            candidate=replay(paths2)
            if candidate is not None:archive.append(candidate)
        chosen=choose_state(archive,p,*goals)
        if chosen is state or attempts>=max_evaluations:state=chosen;break
        state=chosen
    return state,attempts


def optimize(candidates,p,ticket_budget,key,route_function,candidate_limit=32,width=48,request_limit=20):
    selected=select_candidates(candidates,candidate_limit)
    known={};errors={};queried=set();used_requests=0
    state=None;metrics=[];rounds=0;was_slow=False
    local_evaluations=0
    solver_reports=[]
    context=prepare_search(selected,p)
    identifiers={edge_key(a,b):cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
                 for a in selected for b in selected if a['poi_id']!=b['poi_id']}
    cached_routes=read_cache_many(identifiers.values())
    known.update({pair:cached_routes[identifier] for pair,identifier in identifiers.items() if cached_routes.get(identifier)})
    def resolve(items):
        nonlocal used_requests,was_slow
        def fetch(item):
            pair,a,b,identifier=item
            try:return item,route_function(key,a,b,p['mode'],p['city'],use_cache=False),''
            except AmapError as exc:return item,None,str(exc)
        with ThreadPoolExecutor(max_workers=3) as pool:responses=list(pool.map(fetch,items))
        for (pair,a,b,identifier),leg,error in responses:
            used_requests+=1;queried.add(pair)
            if leg:
                known[pair]=leg;write_cache(identifier,leg,hours=1)
                if leg['minutes']>(60 if p['mode']=='walking' else 90):was_slow=True
            else:errors[pair]=error
    # 先核验少量高分景点的近邻边，避免粗估偏大导致可行的半日组合从未被查询。
    probes=[];seen=set()
    for a in selected[:4]:
        nearby=sorted((b for b in selected if b['poi_id']!=a['poi_id']),key=lambda b:estimate_leg(a,b,p['mode'])['km'])[:2]
        for b in nearby:
            pair=edge_key(a,b)
            if pair in seen:continue
            seen.add(pair)
            identifier=cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
            cached=known.get(pair)
            if cached:known[pair]=cached
            elif len(probes)<min(8,request_limit//2):probes.append((pair,a,b,identifier))
    if probes:resolve(probes)
    # 重算后补核验新选中的边；达到上限后只在已核验或已明确失败的边中重排。
    for _ in range(5):
        rounds+=1
        state,metrics=search(selected,p,ticket_budget,known,errors,closed=used_requests>=request_limit,width=width,context=context)
        if rounds==1:
            state,attempts=improve(state,selected,p,ticket_budget,known,errors,closed=used_requests>=request_limit,max_evaluations=600)
            local_evaluations+=attempts
        if rounds==1:
            from .path_solver import solve_paths
            state,solver_report=solve_paths(selected,p,ticket_budget,known,errors,used_requests>=request_limit,state)
            solver_reports.append(solver_report)
        pairs={edge_key(a,b):(a,b) for path in state.days for a,b in zip(path,path[1:])}
        pending=[]
        for pair,(a,b) in pairs.items():
            if pair in known or pair in errors:continue
            identifier=cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
            cached=known.get(pair)
            if cached:known[pair]=cached
            elif used_requests+len(pending)<request_limit:pending.append((pair,a,b,identifier))
        if not pending:
            # 缓存也可能改变时长，因此再用真实时长排一次，然后终检。
            if not any(k in pairs for k in known):break
            refreshed,_=search(selected,p,ticket_budget,known,errors,closed=used_requests>=request_limit,width=width,context=context)
            new_pairs={edge_key(a,b) for path in refreshed.days for a,b in zip(path,path[1:])}
            if all(k in known or k in errors for k in new_pairs):state=refreshed;break
            if used_requests>=request_limit:state=refreshed;break
            state=refreshed
            continue
        resolve(pending)
    # 最终只用已有核验结果/明确失败的边，避免把尚未查询的新边标成已核验。
    state,metrics=search(selected,p,ticket_budget,known,errors,closed=True,width=width,context=context)
    state,attempts=improve(state,selected,p,ticket_budget,known,errors,closed=True,max_evaluations=600)
    local_evaluations+=attempts
    from .path_solver import solve_paths
    state,solver_report=solve_paths(selected,p,ticket_budget,known,errors,True,state)
    solver_reports.append(solver_report)
    days=[];verified=unverified=0
    for index,path in enumerate(state.days):
        start,end=day_window(p,index)
        stops=list(path)
        for stop in stops:
            leg=stop.get('leg')
            if leg:
                if leg.get('verified'):verified+=1
                else:unverified+=1
        date=p['start_date']+timedelta(days=index)
        if start>=end:reason='本日处于去程在途或返程窗口内，没有足够的景点游玩时间。'
        elif len(stops)<(2 if p['pace']=='relaxed' else 3):
            reason='在可用时段、预算、开放时间及通勤限制内选择了当前组合；每日数量为上限，不强行凑满。'
        else:reason='已达到本日所选游玩节奏的景点数量上限。'
        days.append({'number':index+1,'date':date.isoformat(),'stops':stops,'available_start':time_text(start) if start<1440 else '尚未抵达',
                     'available_end':time_text(max(0,end)),'available_text':('在途或返程预留，本日没有可用游玩时段' if start>=end else time_text(start)+'—'+time_text(end)),'available_minutes':max(0,end-start),'schedule_reason':reason,
                     'target':2 if p['pace']=='relaxed' else 3,'travel_minutes':sum(s['leg']['minutes'] for s in stops if s.get('leg'))})
    return {'days':days,'verified_legs':verified,'unverified_legs':unverified,'slow_repaired':was_slow,
            'optimizer':{'method':'日路径整数规划（PuLP/CBC）＋时间窗核验＋真实路线反馈','solver_reports':solver_reports,'beam_width':width,'local_evaluations':local_evaluations,'budget_strategy':p.get('budget_strategy','quality'),'quality_tolerance_pct':0,
             'considered_candidates':len(selected),'available_candidates':len(candidates),'route_requests':used_requests,'rounds':rounds,
             'objective_score':round(state.utility,2),'guarantee':'有界候选与搜索范围内的可行方案，不保证全局最优'}}
