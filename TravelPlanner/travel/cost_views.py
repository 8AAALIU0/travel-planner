import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_GET
from .cost_forms import QuoteForm, CostBudgetForm
from .cost_service import QuoteError, run_query, load_saved_ticket, money
from .planner_views import INPUT, RESULT

QUOTE = 'travel_quote_v6'
BUDGET = 'travel_cost_budget_v6'
TICKETS = 'travel_ticket_refs_v6'


def serial(d):
    return {k:v if isinstance(v,(int,list,bool)) or v is None else str(v) for k,v in d.items()}


def budget_totals(d):
    hotel = d['hotel_total'] if d.get('hotel_total') is not None else d['hotel']*d['rooms']*d['nights']
    return {'hotel':hotel,'outbound':d['outbound']*d['people'],'inbound':d['inbound']*d['people'],
            'food':d['food']*d['people']*d['days'],'transport':d['transport']*d['people']*d['days'],
            'reserve':(d['budget']*Decimal('.1')).quantize(Decimal('.01'))}


@require_http_methods(['GET','POST'])
def costs(request):
    preferences = request.session.get(INPUT,{})
    initial = {**preferences, **request.session.get(BUDGET,{})}
    initial.setdefault('nights', max(0,int(initial.get('days',3))-1))
    if 'intercity' in initial and 'outbound' not in initial:
        initial['outbound'] = str(Decimal(initial['intercity'])/Decimal(str(initial.get('people',2))))
        initial['inbound'] = '0'
    bform = CostBudgetForm(initial=initial, auto_id="budget_%s")
    qinitial = request.session.get('travel_quote_input_v6',{})
    if not qinitial:
        qinitial = {'city':initial.get('city','上海'),'day':initial.get('start_date',timezone.localdate()),'adults':initial.get('people',2)}
    qform = QuoteForm(initial=qinitial)
    record = request.session.get(QUOTE)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'query':
                qform = QuoteForm(request.POST)
                if qform.is_valid():
                    record, cached = run_query(qform.cleaned_data['kind'],qform.arguments())
                    request.session[QUOTE] = record
                    request.session['travel_quote_input_v6'] = serial(qform.cleaned_data)
                    messages.success(request,'已读取15分钟内的缓存，未重新调用。' if cached else '查询完成。请核对日期、票型与适用条件。')
                    return redirect('travel_costs')
            elif action == 'saved':
                record = load_saved_ticket(request.POST.get('filename',''))
                request.session[QUOTE] = record
                messages.info(request,'已打开历史结果，不是重新查询。可下载原始数据核对。')
                return redirect('travel_costs')
            elif action == 'budget':
                bform = CostBudgetForm(request.POST, auto_id="budget_%s")
                if bform.is_valid():
                    values = bform.cleaned_data
                    request.session[BUDGET] = serial(values)
                    merged = {**preferences, **serial(values)}
                    merged.update(intercity=str((values['outbound']+values['inbound'])*values['people']))
                    merged.setdefault('interests',['nature','culture'])
                    merged.setdefault('pace','relaxed')
                    merged.setdefault('mode','transit')
                    request.session[INPUT] = merged
                    request.session.pop(RESULT,None)
                    messages.success(request,'费用条件已带入规划页。核对兴趣和节奏后，点击“生成我的旅行方案”，旧方案已清除以免混淆。')
                    return redirect('travel_planner')
            elif action == 'adopt':
                if not record or request.POST.get('confirm') != 'yes':
                    raise QuoteError('请先查询，并勾选“已核对票型、人数与日期，仅用于预算参考”。')
                try:
                    row = record['rows'][int(request.POST.get('row','-1'))]
                    if int(request.POST.get('row','-1')) < 0: raise IndexError
                except (ValueError,IndexError,KeyError):
                    raise QuoteError('请重新选择一条有效结果。') from None
                if not row.get('usable'):
                    raise QuoteError('这条结果日期、适用人群或余票不明确，不能自动带入预算。')
                kind = record['kind']
                state = dict(request.session.get(BUDGET,{}))
                target = request.POST.get('target','')
                if kind in ('train','flight') and target in ('outbound','inbound'):
                    stamp = parse_datetime(record.get('queried_at',''))
                    if stamp is None or timezone.is_naive(stamp) or not 0 <= (timezone.now()-stamp).total_seconds() <= 900:
                        raise QuoteError('这条交通报价已超过15分钟或时间不明，请重新查询后再填入预算。')
                    # 只带入单人金额，不把去程当成往返；来源上下文供核对。
                    state[target] = row['unit']
                    sources = request.session.get('travel_cost_sources_v6',{})
                    sources[target] = {'name':row['name'],'args':record['args'],'queried_at':record['queried_at']}
                    request.session['travel_cost_sources_v6'] = sources
                    request.session[BUDGET] = state
                    messages.info(request,'已填入单人'+('去程' if target=='outbound' else '返程')+'预算。请在下方核对路线、日期和人数，再带入规划；没有自动补算另一程。')
                elif kind == 'ticket':
                    args = record['args']
                    ref = {'name':args['scenic_name'],'date':args['depart_date'],'unit':row['unit'],'label':row['name'],
                           'queried_at':record['queried_at'],'priceday':row['raw'].get('departsDate','')}
                    refs = request.session.get(TICKETS,[])
                    refs = [x for x in refs if (x['name'],x['date']) != (ref['name'],ref['date'])]
                    request.session[TICKETS] = (refs+[ref])[-20:]
                    request.session.pop(RESULT,None)
                    messages.info(request,'已记住此票种。重新生成时，仅同名景点且安排日期一致才使用；预留金额至少为该起价，仍不视为当天实价。')
                else:
                    raise QuoteError('酒店最低价的计价单位尚待核实，请在下方住宿预算中手工填写每晚估算或确认后的总额。')
                return redirect('travel_costs')
            elif action == 'clear_tickets':
                request.session.pop(TICKETS,None)
                request.session.pop(RESULT,None)
                messages.info(request,'已清除选定票种，重新生成时恢复资料库参考或未知票价预留。')
                return redirect('travel_costs')
        except QuoteError as exc:
            messages.error(request,str(exc))
    bound = CostBudgetForm({name: initial.get(name,field.initial() if callable(field.initial) else field.initial) for name,field in CostBudgetForm.base_fields.items()})
    totals = None
    if bound.is_valid():
        totals = budget_totals(bound.cleaned_data)
        totals['subtotal'] = sum(totals.values())
        totals['remaining'] = bound.cleaned_data['budget']-totals['subtotal']
    folder = Path(settings.BASE_DIR)/'ticket_results'
    files = sorted((p.name for p in folder.glob('tuniu_*.json') if p.is_file()), reverse=True)[:20] if folder.is_dir() else []
    plan = request.session.get(RESULT) or {}
    stops = [{'name':s['name'],'date':day['date']} for day in plan.get('days',[]) for s in day['stops']]
    import os
    return render(request,'travel/costs.html',{'qform':qform,'bform':bform,'record':record,'files':files,'totals':totals,
                'ticket_refs':request.session.get(TICKETS,[]),'sources':request.session.get('travel_cost_sources_v6',{}),
                'stops':stops,'tuniu_ready':bool(os.environ.get('TUNIU_API_KEY'))})


@require_GET
def export_quote(request):
    record = request.session.get(QUOTE)
    if not record:
        return HttpResponse('请先查询或打开历史结果。',status=404)
    response = HttpResponse(json.dumps(record,ensure_ascii=False,indent=2),content_type='application/json; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="travel_quote.json"'
    return response
