import copy
import os
import threading
import uuid
from datetime import timedelta
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render,redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods,require_POST,require_GET
from .journey_forms import JourneyForm,TransportPreferenceForm
from .journey_service import initial_job,advance,STAGES
from .models import PlannerCache
from .planner_api import cache_key

JOB='travel_journey_job_v7'
_lock=threading.Lock()


def get_job(request):
    key=request.session.get(JOB)
    if not key:return None
    return PlannerCache.objects.filter(cache_key=key,expires_at__gt=timezone.now()).first()


@require_http_methods(['GET','POST'])
def journey(request):
    item=get_job(request)
    if request.method=='POST':
        if item and not item.payload.get('done') and not item.payload.get('failed'):
            messages.info(request,'当前方案仍在生成，下方会继续完成。')
            return redirect('travel_journey')
        form=JourneyForm(request.POST)
        if form.is_valid():
            job=initial_job(form.cleaned_data)
            key=cache_key('journey-v7',uuid.uuid4().hex)
            PlannerCache.objects.create(cache_key=key,payload=job,expires_at=timezone.now()+timedelta(hours=6))
            request.session[JOB]=key
            return redirect('travel_journey')
    else:
        initial=copy.deepcopy(item.payload['input']) if item else {}
        if isinstance(initial.get('keywords'),list):initial['keywords']='、'.join(initial['keywords'])
        form=JourneyForm(initial=initial)
    from .presentation import present_job
    job=present_job(item.payload) if item else None
    return render(request,'travel/journey.html',{'form':form,'job':job,'plan':job.get('plan') if job and job.get('done') else None,
        'stages':STAGES,'main_fields':['origin','city','start_date','days','people','budget','keywords','stay_kind','hotel_preference','stay_area','travel_kind','travel_priority','pace','mode'],
        'transport_form':TransportPreferenceForm(initial=job.get('input',{}) if job else {},prefix='transport'),'extra_fields':['ticket_policy','minimum_sights','minimum_play_days','allow_overnight','seat_preference','max_travel_hours','transport_budget_pct','rooms','nights','hotel','food','transport','unknown_ticket','fallback_return']})


@require_POST
def journey_step(request):
    item=get_job(request)
    if not item:return JsonResponse({'error':'方案已过期，请重新生成。'},status=404)
    job=item.payload
    if job.get('done') or job.get('failed'):return JsonResponse({'done':True})
    if job.get('running'):
        if timezone.now().timestamp()-job.get('started',0)>600:
            job.update(failed=True,running=False)
            item.payload=job;item.save(update_fields=['payload'])
            return JsonResponse({'done':True})
        return JsonResponse({'running':True})
    try:index=int(request.POST.get('index','-1'))
    except ValueError:return JsonResponse({'error':'步骤无效'},status=400)
    if index!=job['index']:return JsonResponse({'reload':True})
    claimed=copy.deepcopy(job);claimed.update(running=True,started=timezone.now().timestamp())
    # 条件更新认领任务；双击、两个标签页不会同时执行同一步查询。
    acquired=PlannerCache.objects.filter(pk=item.pk,payload__index=index,payload__running=False).update(payload=claimed)
    if not acquired:return JsonResponse({'running':True})
    try:
        updated=advance(job,os.environ.get('TRAVEL_AMAP_KEY',''))
    except Exception:
        # 不把环境、凭据或堆栈写进网页；保留已有步骤供检查。
        updated=copy.deepcopy(job)
        updated.update(failed=True,running=False)
        updated['warnings'].append('本次生成在合并过程中遇到异常，请保留页面截图；已有参考没有被伪装成完整方案。')
    PlannerCache.objects.filter(pk=item.pk).update(payload=updated)
    return JsonResponse({'reload':True})


@require_POST
def journey_reset(request):
    item=get_job(request)
    if item and item.payload.get('running'):
        messages.info(request,'当前步骤正在查询，请完成后再重新生成。')
    else:request.session.pop(JOB,None)
    return redirect('travel_journey')


@require_GET
def journey_export(request):
    item=get_job(request)
    if not item or not item.payload.get('done'):return HttpResponse('请先生成完整方案。',status=404)
    import json
    r=HttpResponse(json.dumps(item.payload,ensure_ascii=False,indent=2),content_type='application/json; charset=utf-8')
    r['Content-Disposition']='attachment; filename="journey_plan.json"'
    return r


@require_POST
def retry_transport(request):
    item=get_job(request)
    if not item:return JsonResponse({'error':'请先生成方案。'},status=404)
    job=copy.deepcopy(item.payload)
    if job.get('running') or (not job.get('done') and not job.get('failed')):
        messages.info(request,'当前流程仍在执行，请完成后再重查。')
    else:
        job.update(index=1,running=False,done=False,failed=False,prices={},warnings=[],retry_transport_only=True,
                   steps=[{'name':'保留原有景点和住宿','message':'仅重新查询往返交通，之后更新时段和预算。'}])
        job.pop('transport_diagnostics',None)
        job.pop('transport_pools',None)
        job.pop('transport_options',None)
        job.pop('joint_comparison',None)
        job.pop('sightseeing_check',None)
        item.payload=job;item.save(update_fields=['payload'])
    return redirect('travel_journey')


def route_context(request,day,stop):
    item=get_job(request)
    if not item or not item.payload.get('done'):
        return None,None,None
    try:
        stops=item.payload['plan']['days'][day]['stops']
        if stop<1:raise IndexError
        return item,stops[stop-1],stops[stop]
    except (IndexError,KeyError,TypeError):return None,None,None


@require_POST
def route_detail(request,day,stop):
    from .planner_api import route_between
    from .amap_client import AmapError
    from django.urls import reverse
    item,start,end=route_context(request,day,stop)
    if not item:return JsonResponse({'error':'这段路线不存在或不属于当前方案。'},status=404)
    if item.payload.get('running'):return JsonResponse({'error':'方案正在更新，请稍后查看。'},status=409)
    leg=end.get('leg') or {}
    try:
        if not leg.get('detail_version'):
            leg=route_between(os.environ.get('TRAVEL_AMAP_KEY',''),start,end,item.payload['plan']['mode'],item.payload['plan']['city'])
            item.payload['plan']['days'][day]['stops'][stop]['leg']=leg
            item.save(update_fields=['payload'])
        return JsonResponse({'leg':leg,'from':start['name'],'to':end['name'],
                             'map_url':reverse('travel_route_map',args=[day,stop])})
    except AmapError as exc:return JsonResponse({'error':str(exc)},status=502)


@require_GET
def route_map(request,day,stop):
    from .route_details import static_map
    from .amap_client import AmapError
    item,start,end=route_context(request,day,stop)
    if not item:return JsonResponse({'error':'这段路线不存在或不属于当前方案。'},status=404)
    try:
        image,mime=static_map(os.environ.get('TRAVEL_AMAP_KEY',''),end.get('leg') or {},start,end)
        response=HttpResponse(image,content_type=mime)
        response['Cache-Control']='private, max-age=300'
        response['X-Content-Type-Options']='nosniff'
        return response
    except AmapError as exc:return JsonResponse({'error':str(exc)},status=502)


@require_POST
def optimize_existing(request):
    item=get_job(request)
    if not item:return JsonResponse({'error':'请先生成方案。'},status=404)
    job=copy.deepcopy(item.payload)
    if job.get('running') or (not job.get('done') and not job.get('failed')):
        messages.info(request,'当前流程仍在执行，请完成后再优化。')
    else:
        job.update(index=6,running=False,done=False,failed=False,warnings=[],
                   steps=job.get('steps',[])[:6])
        item.payload=job;item.save(update_fields=['payload'])
    return redirect('travel_journey')


@require_POST
def transport_preferences(request):
    item=get_job(request)
    if not item:return JsonResponse({'error':'请先生成一份方案。'},status=404)
    job=copy.deepcopy(item.payload)
    if job.get('running') or not (job.get('done') or job.get('failed')):
        messages.info(request,'当前流程仍在生成，请完成后再修改交通偏好。')
        return redirect('travel_journey')
    form=TransportPreferenceForm(request.POST,prefix='transport')
    if not form.is_valid():
        messages.error(request,'交通偏好未保存：'+str(form.errors.as_text()))
        return redirect('travel_journey')
    job['input'].update(form.cleaned_data)
    job.update(index=1,running=False,done=False,failed=False,prices={},warnings=[],retry_transport_only=True,
               steps=[{'name':'更新交通偏好','message':'保留原有城市、住宿和票种资料，重新比较往返交通并重排景点。'}])
    for key in ('transport_diagnostics','transport_pools','transport_options','joint_comparison','sightseeing_check'):job.pop(key,None)
    item.payload=job;item.save(update_fields=['payload'])
    return redirect('travel_journey')


@require_POST
def select_transport(request):
    from .transport_policy import timestamp,budget_limit
    from .journey_service import restore,lodging_window
    from decimal import Decimal
    item=get_job(request)
    if not item:return JsonResponse({'error':'请先生成方案。'},status=404)
    job=copy.deepcopy(item.payload)
    if job.get('running') or not job.get('done'):
        messages.info(request,'当前流程尚未完成，请稍后选择。')
        return redirect('travel_journey')
    leg=request.POST.get('leg')
    try:
        index=int(request.POST.get('index','-1'))
        if leg not in ('outbound','inbound') or index<0:raise ValueError
        selected=copy.deepcopy(job['transport_options'][leg][index])
    except (ValueError,KeyError,IndexError):
        messages.error(request,'该候选不存在，请重新查询。');return redirect('travel_journey')
    stamp=timestamp(selected.get('queried_at'))
    if not stamp or timezone.is_naive(stamp) or not 0<=(timezone.now()-stamp).total_seconds()<=900:
        messages.info(request,'这条报价已超过15分钟或时间不明，请先重查往返交通。');return redirect('travel_journey')
    prices={**job.get('prices',{}),leg:selected}
    if prices.get('outbound') and prices.get('inbound'):
        out,back=prices['outbound'],prices['inbound']
        arrive=timestamp(out['raw'].get('arrivalTime'));leave=timestamp(back['raw'].get('departureTime'))
        try:gap=(leave-arrive).total_seconds()/60
        except TypeError:gap=-1
        if gap<(300 if 'flight' in (out.get('transport_kind'),back.get('transport_kind')) else 180):
            messages.error(request,'这条候选与另一程时间无法衔接，请更换另一程或重新查询。');return redirect('travel_journey')
        p=restore(job['input'])
        if p.get('transport_budget_pct') is not None and Decimal(out['unit'])+Decimal(back['unit'])>budget_limit(p):
            messages.error(request,'这组往返费用超出设定的交通预算占比，请修改偏好或选择其他候选。');return redirect('travel_journey')
    p=restore(job['input'])
    old_lodging=job.get('lodging') or lodging_window(job,p)
    job['prices']=prices
    job.pop('joint_comparison',None)
    job.pop('sightseeing_check',None)
    new_lodging=lodging_window(job,p)
    changed=(old_lodging['checkIn'],old_lodging['checkOut'])!=(new_lodging['checkIn'],new_lodging['checkOut'])
    start_step=3  # 交通改变后同步核对住宿参照点及当前景点门票；相同酒店条件会用缓存。
    job.update(index=start_step,running=False,done=False,failed=False,warnings=[],retry_transport_only=True,steps=job.get('steps',[])[:start_step])
    item.payload=job;item.save(update_fields=['payload'])
    return redirect('travel_journey')


@require_POST
def retry_hotels(request):
    item=get_job(request)
    if not item:return JsonResponse({'error':'请先生成方案。'},status=404)
    job=copy.deepcopy(item.payload)
    if job.get('running') or not (job.get('done') or job.get('failed')):
        messages.info(request,'当前流程仍在执行，请完成后重查酒店。')
    elif job['input'].get('stay_kind')=='free':
        messages.info(request,'当前选择了已有免费住所；请先在上方改为需要酒店并重新生成。')
    else:
        job.update(index=3,done=False,failed=False,running=False,retry_hotel_only=True,warnings=[],steps=[])
        job.pop('hotel_search',None)
        item.payload=job;item.save(update_fields=['payload'])
    return redirect('travel_journey')
