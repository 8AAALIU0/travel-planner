"""在有限往返候选中，先检查实际景点可排性，再按交通偏好选择。"""
import copy
from datetime import timedelta
from decimal import Decimal
from .transport_policy import timestamp,deduplicate,budget_limit,score_values,reason,PRIORITIES
from .planner_service import day_window,fetch_pool,enrich
from .itinerary_optimizer import search


def goals(p):
    cap=2 if p['pace']=='relaxed' else 3
    return p.get('minimum_sights') or min(3,cap*p['days']),p.get('minimum_play_days') or p['days']


def parameters(p,out,back):
    q=copy.deepcopy(p)
    arrival=timestamp(out['raw']['arrivalTime']);departure=timestamp(back['raw']['departureTime'])
    q['arrival_day_offset']=(arrival.date()-p['start_date']).days
    q['arrival_minutes']=arrival.hour*60+arrival.minute+(120 if out['transport_kind']=='flight' else 90)
    q['return_cutoff']=departure.hour*60+departure.minute-(180 if back['transport_kind']=='flight' else 120)
    if p.get('stay_kind')=='free':q['nights']=0
    elif p.get('nights') is None:q['nights']=max(0,(departure.date()-max(p['start_date'],arrival.date())).days)
    q['intercity']=(Decimal(out['unit'])+Decimal(back['unit']))*p['people']
    q['_minimum_sights'],q['_minimum_play_days']=goals(p)
    return q


def windows(p):
    return [max(0,b-a) for a,b in (day_window(p,i) for i in range(p['days']))]


def remaining(p):
    from .stay_policy import lodging_cost
    nights=p['nights'] if p.get('nights') is not None else p['days']-1
    return p['budget']*Decimal('.9')-p['intercity']-lodging_cost(p,nights)-(p['food']+p['transport'])*p['people']*p['days']


def snapshot(state,p):
    counts=[len(path) for path in state.days]
    target_sights,target_days=goals(p)
    count=sum(counts);days=sum(bool(n) for n in counts)
    return {'stop_count':count,'play_days':days,'counts':counts,'counts_text':' / '.join(map(str,counts)),
            'requested_sights':target_sights,'requested_play_days':target_days,
            'met':count>=target_sights and days>=target_days,'available_minutes':sum(windows(p))}


def select_joint(outbound,inbound,p,key,verifier=None):
    """最多12组离线景点排程，不为每组重复调用高德路线。最终仍须真实路线核验。"""
    pairs=[];priority=p.get('travel_priority') or 'balanced';wanted,play_days=goals(p)
    for out in deduplicate(outbound):
        for back in deduplicate(inbound):
            arrive=timestamp(out['raw']['arrivalTime']);leave=timestamp(back['raw']['departureTime'])
            try:gap=(leave-arrive).total_seconds()/60
            except TypeError:continue
            if gap<(300 if 'flight' in (out['transport_kind'],back['transport_kind']) else 180):continue
            q=parameters(p,out,back);fare=Decimal(out['unit'])+Decimal(back['unit'])
            if p.get('transport_budget_pct') is not None and fare>budget_limit(p):continue
            durations=windows(q)
            capacity=sum(min(2 if p['pace']=='relaxed' else 3,m//90) for m in durations)
            viable_days=sum(m>=90 for m in durations)
            pairs.append({'out':out,'back':back,'p':q,'fare':fare,
                'travel':out['door_minutes']+back['door_minutes'],'loss':out['play_loss']+back['play_loss'],
                'quick':(int(remaining(q)>=0),min(viable_days,play_days),min(capacity,wanted)),
                'minutes':sum(durations)})
    if not pairs:return None,None,{'error':'当前往返候选无法同时满足时间衔接与交通预算限制。'}
    # 从不同价位/耗时及可用时段保留代表组，避免只检查便宜的早返车次。
    shortlist=[];seen=set()
    for order in (sorted(pairs,key=lambda x:(tuple(-v for v in x['quick']),x['fare'],x['travel'])),
                  sorted(pairs,key=lambda x:(tuple(-v for v in x['quick']),x['travel'],x['fare'])),
                  sorted(pairs,key=lambda x:(tuple(-v for v in x['quick']),-x['minutes'],x['fare']))):
        for item in order[:4]:
            token=(item['out']['name'],item['out']['raw']['departureTime'],item['back']['name'],item['back']['raw']['departureTime'])
            if token not in seen:shortlist.append(item);seen.add(token)
    rows,_,_=fetch_pool(p['city'],p['interests'],key,keywords=p.get('keywords'))
    candidates=enrich(rows,p)[:24]
    # 复用已有真实路线缓存；尚无缓存的边仅用于候选比较中的估算，最终会再核验。
    from .planner_api import cache_key,read_cache
    known={}
    for a in candidates:
        for b in candidates:
            if a['poi_id']==b['poi_id']:continue
            identifier=cache_key('route-v8',[f"{a['longitude']:.6f},{a['latitude']:.6f}",f"{b['longitude']:.6f},{b['latitude']:.6f}",p['mode'],p['city']])
            cached=read_cache(identifier)
            if cached:known[(a['poi_id'],b['poi_id'])]=cached
    for item in shortlist:
        q=item['p'];state,_=search(candidates,q,max(Decimal(0),remaining(q)),known=known,width=16)
        item['evaluation']=snapshot(state,q)
        if remaining(q)<0:item['evaluation'].update(met=False,stop_count=0,play_days=0,counts=[0]*p['days'],counts_text=' / '.join(['0']*p['days']))
    for score,item in zip(score_values([(float(i['fare']),i['travel'],i['loss']) for i in shortlist],priority),shortlist):item['score']=score
    def order(item):
        e=item['evaluation']
        transport=(float(item['fare']),item['travel']) if priority=='economy' else (item['score'],float(item['fare']))
        return (-int(e['met']),-min(e['play_days'],play_days),-min(e['stop_count'],wanted),*transport)
    shortlist.sort(key=order)
    # 优先核验预排最合适的一组；不满足目标时再核验第二组，限制额外接口消耗。
    checked=[]
    if verifier:
        for item in shortlist[:2]:
            plan=verifier(item['p'],key)
            e=validate_plan(plan,item['p'])
            item['evaluation'].update(e,preview=False)
            checked.append(item)
            if e['met']:break
        checked.sort(key=order)
        best=checked[0]
    else:best=shortlist[0]
    out,back=copy.deepcopy(best['out']),copy.deepcopy(best['back'])
    for row in (out,back):row.update(priority_label=PRIORITIES[priority],selection_reason='先检查景点数量与游玩天数，再按交通偏好比较。'+reason(row,priority))
    report=dict(best['evaluation'],compared_pairs=len(pairs),scheduled_pairs=len(shortlist),
                preview=not bool(verifier),verified_pairs=len(checked),method='往返组合＋景点时间窗和预算联合比较（预排程），最终核验高德路线。',
                options=[{'outbound':i['out']['name'],'inbound':i['back']['name'],'fare':str(i['fare']),**i['evaluation']} for i in shortlist[:6]])
    return out,back,report


def validate_plan(plan,p,confirmed=True):
    wanted,days=goals(p)
    counts=[len(d['stops']) for d in plan.get('days',[])]
    count=sum(counts);actual=sum(bool(n) for n in counts)
    report={'stop_count':count,'play_days':actual,'counts':counts,'counts_text':' / '.join(map(str,counts)),
            'requested_sights':wanted,'requested_play_days':days,'met':confirmed and count>=wanted and actual>=days,'time_confirmed':confirmed}
    if not confirmed:plan['warnings'].append('部分往返交通时刻未知，景点为暂排，尚不能确认游玩目标已满足。')
    if not report['met']:
        plan['warnings'].append(f'游玩目标未满足：希望至少{wanted}个景点、{days}天有景点；实际{count}个景点、{actual}天。当前有限车次、开放时间、预算或景点通勤无法兼顾，请比较其他交通、放宽条件或增加旅行天数。')
    plan['sightseeing_check']=report
    return report
