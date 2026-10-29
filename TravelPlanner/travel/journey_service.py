"""一套输入生成跨城、住宿、景点与预算；价格未知时保持估算标记。"""
import copy
import time
from datetime import datetime,timedelta,date
from decimal import Decimal
from django.utils import timezone
from . import cost_service
from .cost_service import QuoteError, money, seat_count
from .planner_service import build_plan

STAGES=['寻找符合兴趣的景点并安排初稿','查询去程交通','查询返程交通','按住宿需求筛选酒店','逐个核对行程中的门票','重排并补核新增景点门票','合并路线、时段与全部费用']


def restore(data):
    p=copy.deepcopy(data)
    p['start_date']=date.fromisoformat(p['start_date'])
    for field in ('budget','hotel','food','transport','unknown_ticket','fallback_return','intercity'):
        if field in p:p[field]=Decimal(str(p[field]))
    p.setdefault('interests',['nature','culture'])
    p['hotel_total']=None
    from .stay_policy import apply_stay
    p=apply_stay(p)
    from .joint_planner import goals
    p['_minimum_sights'],p['_minimum_play_days']=goals(p)
    return p


def initial_job(values):
    p={k:v if isinstance(v,(int,list)) or v is None else str(v) for k,v in values.items()}
    p['intercity']=str(values['fallback_return']*values['people'])
    return {'input':p,'index':0,'running':False,'done':False,'steps':[], 'prices':{},'hotels':[], 'ticket_options':[], 'warnings':[], 'created_at':timezone.now().isoformat()}


def query(kind,args):
    # 自动流程自行遵守调用间隔，不要求用户手动等待再点下一项。
    wait=13-(time.monotonic()-cost_service._last_call)
    if wait>0:time.sleep(min(wait+.05,13.1))
    record,_=cost_service.run_query(kind,args)
    return record


def minute(text):
    try:
        t=datetime.fromisoformat(str(text))
        return t.hour*60+t.minute
    except (ValueError,TypeError):return None


def pick_transport(record,people,leg,price_cap=None):
    # 保留旧模块调用接口；主流程使用下方的往返组合策略。
    from .transport_policy import prepare,choose_one
    return choose_one(prepare(record,people,leg),priority='fast',price_cap=price_cap)


def lodging_window(job,p):
    first=p['start_date']
    last=first+timedelta(days=p['days']-1)
    outbound=job.get('prices',{}).get('outbound')
    inbound=job.get('prices',{}).get('inbound')
    try:
        if outbound:first=max(first,date.fromisoformat(outbound['raw']['arrivalTime'][:10]))
        if inbound:last=date.fromisoformat(inbound['raw']['departureTime'][:10])
    except (ValueError,KeyError):pass
    available=max(0,(last-first).days)
    manual=p.get('nights') is not None
    nights=0 if p.get('stay_kind')=='free' else p['nights'] if manual else available
    notes=[]
    if manual and nights>available:notes.append('手动住宿晚数超过车次对应的目的地住宿晚数，请核对是否需要提前订房或调整日期。')
    return {'checkIn':first.isoformat(),'checkOut':(first+timedelta(days=nights)).isoformat(),'nights':nights,'manual':manual,'notes':notes}



def final_parameters(job,p):
    q=copy.deepcopy(p);lodging=lodging_window(job,q);q['nights']=lodging['nights']
    out,back=job.get('prices',{}).get('outbound'),job.get('prices',{}).get('inbound')
    q['intercity']=sum((Decimal(r['unit']) if r else q['fallback_return']/2 for r in (out,back)),Decimal(0))*q['people']
    if out:
        q['arrival_minutes']=minute(out['raw']['arrivalTime'])+(120 if out.get('transport_kind')=='flight' else 90)
        q['arrival_day_offset']=(date.fromisoformat(out['raw']['arrivalTime'][:10])-q['start_date']).days
    if back:q['return_cutoff']=minute(back['raw']['departureTime'])-(180 if back.get('transport_kind')=='flight' else 120)
    q['ticket_refs']=job.get('ticket_refs',[])
    return q


def ticket_stage(job,p,key,stage):
    from .ticket_budgeting import official,options_for
    from django.utils.dateparse import parse_datetime
    field='ticket_queue_'+str(stage)
    if field not in job:
        draft=build_plan(final_parameters(job,p),key)
        checked=set(job.get('ticket_checked',[]))
        job[field]=[{'name':s['name'],'date':d['date'],'city':p['city']} for d in draft.get('days',[]) for s in d['stops'] if s['name']+'|'+d['date'] not in checked][:15]
    queue=job[field]
    if len(job.get('ticket_checked',[]))>=15:return True,'本轮已处理15个景点日期，其他项目明确使用估算。'
    if not queue:return True,'当前安排的景点票价已处理；未查到的项目保留分类估算。'
    target=queue.pop(0);token=target['name']+'|'+target['date'];message=''
    ref=official(target,date.fromisoformat(target['date']))
    existing=next((r for r in job.get('ticket_refs',[]) if r.get('name')==target['name'] and r.get('date')==target['date']),None)
    stamp=parse_datetime(existing.get('queried_at','')) if existing else None
    fresh=stamp and timezone.is_aware(stamp) and 0<=(timezone.now()-stamp).total_seconds()<=86400
    if ref:message=target['name']+'：使用已核对的官方资料参考，保留来源和核对日。'
    elif fresh:message=target['name']+'：沿用同景点同日期的近期成人票参考。'
    elif job.get('ticket_queries_paused'):message=target['name']+'：平台查询暂不可用，明确使用分类估算。'
    else:
        try:
            record=query('ticket',{'scenic_name':target['name'],'depart_date':target['date']})
            rows=options_for(record,target)
            options=[{'name':r['name'],'unit':r['unit'],'info':r['info']} for r in rows][:4]
            job.setdefault('ticket_options',[]).append({**target,'options':options,'queried_at':record['queried_at']})
            if rows:
                # 优先普通单票，避免将餐饮/住宿套票当成景区单门票。
                plain=[r for r in rows if not any(w in r['name'] for w in ['自助餐','餐饮','酒店','住宿','套票','联票'])]
                selected=min(plain or rows,key=lambda r:Decimal(r['unit']))
                new={**target,'unit':selected['unit'],'market':selected['raw'].get('priceMarket'),'label':selected['name'],'priceday':selected['raw'].get('departsDate',''),'queried_at':record['queried_at'],'auto':True}
                job['ticket_refs']=[r for r in job.get('ticket_refs',[]) if (r.get('name'),r.get('date'))!=(target['name'],target['date'])]+[new]
                message=target['name']+'：取得成人票起价参考，按已返回起价/门市价及预算余量估算。'
            else:message=target['name']+'：未取得匹配日期和身份的成人票，使用分类估算。'
        except QuoteError as exc:
            message=target['name']+'：'+str(exc)+'；使用分类估算。'
            if any(w in str(exc) for w in ['密钥','额度','频率','权限','429']):job['ticket_queries_paused']=True
            job['warnings'].append(message)
    job.setdefault('ticket_checked',[]).append(token)
    if len(job.get('ticket_checked',[]))>=15:queue.clear()
    return not queue,message+(' 尚余'+str(len(queue))+'个待核对。' if queue else '')

def advance(job, amap_key):
    job=copy.deepcopy(job)
    stage=job['index'];p=restore(job['input']);message=''
    try:
        if stage==0:
            job['draft']=build_plan(p,amap_key)
            job['targets']=[{'name':s['name'],'date':d['date']} for d in job['draft'].get('days',[]) for s in d['stops']][:2]
            message='已按关键词筛选景点并安排初稿。'
        elif stage in (1,2):
            leg='outbound' if stage==1 else 'inbound'
            args={'departureCityName':p['origin'] if stage==1 else p['city'],
                  'arrivalCityName':p['city'] if stage==1 else p['origin'],
                  'departureDate':(p['start_date']+timedelta(days=0 if stage==1 else p['days']-1)).isoformat()}
            args['departureCityName']=args['departureCityName'].strip().removesuffix('市')
            args['arrivalCityName']=args['arrivalCityName'].strip().removesuffix('市')
            from .transport_policy import gather,choose_one,choose_pair,alternatives,budget_limit
            pool,reports,errors=gather(p,leg,args,query)
            job.setdefault('transport_pools',{})[leg]=pool
            job.setdefault('transport_diagnostics',{})[leg]={'kind':p['travel_kind'],'args':args,'rows':sum(r.get('rows',0) for r in reports),
                'eligible_rows':len(pool),'reports':reports,'errors':errors}
            priority=p.get('travel_priority') or 'balanced'
            selected=choose_one(pool,priority,price_cap=budget_limit(p))
            if selected and p.get('transport_budget_pct') is not None and Decimal(selected['unit'])>budget_limit(p):selected=None
            if selected:
                job['prices'][leg]=selected
                message='已按'+selected['priority_label']+'取得'+selected['name']+'，单人参考 ¥'+selected['unit']+'。'
            else:
                job['prices'].pop(leg,None)
                message='查询已完成，但没有满足所选跨夜、座席、余票和时长条件的候选，保留估算。'
                job['warnings'].append(('去程' if stage==1 else '返程')+'没有满足当前交通限制的候选；请核对是否需要放宽条件。')
            if errors:job['warnings'].append('部分交通查询未完成，已使用其余成功结果：'+'；'.join(dict.fromkeys(errors)))
            if stage==2:
                pools=job.get('transport_pools',{})
                if pools.get('outbound') and pools.get('inbound'):
                    from .joint_planner import select_joint
                    from .amap_client import AmapError
                    try:
                        out,back,report=select_joint(pools['outbound'],pools['inbound'],p,amap_key,verifier=build_plan)
                        job['joint_comparison']=report
                        warning=report.get('error','')
                        if not report.get('met') and not warning:warning='当前有限往返候选的景点预排程未达到游玩目标，已保留较接近的组合；最终将再次核验。'
                    except AmapError as exc:
                        out,back,warning=choose_pair(pools['outbound'],pools['inbound'],p)
                        warning+=' 景点联合比较未完成：'+str(exc)+'；本次不能保证游玩目标。'
                        job['joint_comparison']={'error':'景点资料查询未完成，尚未验证游玩目标。'}
                    if out and back:job['prices'].update(outbound=out,inbound=back)
                    else:
                        job['prices'].pop('outbound',None);job['prices'].pop('inbound',None)
                    if warning:job['warnings'].append(warning)
                job['transport_options']={legname:alternatives(items,job['prices'].get(legname),cap=budget_limit(p)) for legname,items in pools.items()}
        elif stage==3:
            lodging=lodging_window(job,p)
            job['lodging']=lodging;job['warnings'].extend(lodging['notes'])
            if not job.get('retry_hotel_only'):
                for field in ('ticket_queue_4','ticket_queue_5','ticket_queries_paused'):job.pop(field,None)
                job['ticket_checked']=[];job['ticket_options']=[]
            if p.get('stay_kind')=='free' or not lodging['nights']:
                job['hotels']=[];job['hotel_search']={'skipped':True,'reason':'已选择不订酒店，住宿费为0。' if p.get('stay_kind')=='free' else '本次无需目的地住宿。'}
                message=job['hotel_search']['reason']
            else:
                from .stay_policy import first_stop,find_hotels,search_args
                draft=build_plan(final_parameters(job,p),amap_key)
                anchor=first_stop(draft)
                signature=['hotel-v13',lodging['checkOut'],search_args(p,lodging,anchor),p.get('hotel_preference') or 'balanced',p['mode'],anchor.get('poi_id') if anchor else '']
                old=job.get('hotel_search',{})
                from django.utils.dateparse import parse_datetime
                stamp=parse_datetime(old.get('queried_at',''))
                fresh=stamp and timezone.is_aware(stamp) and 0<=(timezone.now()-stamp).total_seconds()<=900
                if old.get('signature')==signature and fresh and not job.get('retry_hotel_only'):
                    message='住宿日期、价格与位置条件未变化，沿用近期酒店比较。'
                    job['steps'].append({'name':STAGES[stage],'message':message});job['index']+=1;job['running']=False;return job
                job['hotels']=[]
                job['hotels'],job['hotel_search']=find_hotels(p,lodging,anchor,amap_key,query)
                job['hotel_search']['signature']=signature
                job['hotel_queried_at']=job['hotel_search']['queried_at']
                message='已按每间每晚预算区间和住宿倾向查询；住宿总额仍按设定标准估算。'
                if not job['hotel_search']['eligible']:job['warnings'].append(job['hotel_search']['status'])
        elif stage in (4,5):
            finished,message=ticket_stage(job,p,amap_key,stage)
            if not finished:
                entry={'name':STAGES[stage],'message':message}
                if job['steps'] and job['steps'][-1]['name']==STAGES[stage]:job['steps'][-1]=entry
                else:job['steps'].append(entry)
                job['running']=False
                return job
        else:
            outbound,inbound=job['prices'].get('outbound'),job['prices'].get('inbound')
            # 只查到一程时另一程使用原往返预留的一半，不默认为0。
            lodging=lodging_window(job,p)
            job['lodging']=lodging
            p['nights']=lodging['nights']
            job['warnings'].extend(lodging['notes'])
            single_fallback=p['fallback_return']/2
            p['intercity']=sum((Decimal(r['unit']) if r else single_fallback for r in (outbound,inbound)),Decimal(0))*p['people']
            if p.get('transport_budget_pct') is not None and (not outbound or not inbound):
                job['warnings'].append('部分交通价格未知，交通预算占比尚无法完全核实，缺失部分仍为估算预留。')
            if outbound:
                arrival=minute(outbound['raw'].get('arrivalTime'))
                if arrival is not None:
                    p['arrival_minutes']=arrival+(120 if outbound.get('transport_kind',p['travel_kind'])=='flight' else 90)
                    p['arrival_day_offset']=(date.fromisoformat(outbound['raw']['arrivalTime'][:10])-p['start_date']).days
                    if p['arrival_day_offset']>0:job['warnings'].append('去程为跨夜行程，已从实际抵达日开始安排景点。')
                    if p['arrival_day_offset']>=p['days']:job['warnings'].append('所选去程到达时间已超出旅行日期范围，请增加天数或更换交通方案。')
            if inbound:
                departure=minute(inbound['raw'].get('departureTime'))
                if departure is not None:p['return_cutoff']=departure-(180 if inbound.get('transport_kind',p['travel_kind'])=='flight' else 120)
                back_day=inbound['raw'].get('arrivalTime','')[:10]
                if back_day and back_day>inbound['args']['departureDate']:
                    job['warnings'].append('返程跨夜，实际到家日期为'+back_day+'，晚于所选末日出发日期，请核对总旅行时长。')
            # 复用初稿的景点集合；最终重算预算、日期与真实交通时段。
            p['ticket_refs']=job.get('ticket_refs',[])
            result=build_plan(p,amap_key)
            from .joint_planner import validate_plan
            job['sightseeing_check']=validate_plan(result,p,confirmed=bool(outbound and inbound))
            result['warnings']+=job['warnings']
            result['warnings'].append('报价来自有限查询结果；不是全网最低价或库存保证。市内交通、住宿和未确认门票仍为预算估算。')
            result['warnings'].append('每天景点安排已为去程抵达、返程候车预留时间；酒店、车站和景点接驳时间为估计，具体接驳路线仍需核对。')
            job['plan']=result
            job['breakdown']=[{'name':'去程交通','amount':str((Decimal(outbound['unit']) if outbound else single_fallback)*p['people']),'state':'查询参考' if outbound else '估算预留'},
                              {'name':'返程交通','amount':str((Decimal(inbound['unit']) if inbound else single_fallback)*p['people']),'state':'查询参考' if inbound else '估算预留'},
                              {'name':'住宿','amount':result['costs']['hotel'],'state':'已有免费住所，未计酒店费用' if p.get('stay_kind')=='free' else '每间每晚预算 × 房间数 × 晚数'},
                              {'name':'餐饮','amount':result['costs']['food'],'state':'预算估算'},
                              {'name':'市内交通','amount':result['costs']['transport'],'state':'预算预留'},
                              {'name':'门票','amount':result['costs']['tickets'],'state':'资料参考 / 未知预留'},
                              {'name':'备用金','amount':result['costs']['reserve'],'state':'总预算10%'}]
            job['done']=True
            message='方案已合并完成，请查看每天安排和全体费用。'
    except QuoteError as exc:
        message=str(exc)+' 本项使用估算，继续生成其他部分。'
        if stage in (1,2):
            leg='outbound' if stage==1 else 'inbound'
            job.setdefault('transport_diagnostics',{})[leg]={'error':str(exc),'kind':p['travel_kind'],'args':args}
        job['warnings'].append(STAGES[stage]+'：'+str(exc))
    if stage==3 and job.pop('retry_hotel_only',False):job['index']=5
    job['steps'].append({'name':STAGES[stage],'message':message})
    job['index']+=1
    job['running']=False
    return job
