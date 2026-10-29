"""Small class-demo guard. One worker, fair retries through existing step polling."""
import threading
from datetime import datetime,timezone
from django.conf import settings
from django.core.cache import cache
from django.contrib import messages
from django.http import HttpResponse,JsonResponse
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

_single_query=threading.BoundedSemaphore(1)
_rate_lock=threading.Lock()
_STARTS={'/journey/','/journey/retry-transport/','/journey/transport-preferences/','/journey/retry-hotels/','/journey/select-transport/'}
_COSTLY=_STARTS|{'/journey/step/','/journey/optimize/'}

def _busy(request):
    if request.path=='/journey/step/':return JsonResponse({'running':True,'queued':True})
    messages.info(request,'网站正在处理其他旅行查询，请稍后再试。已有方案仍保留。')
    return redirect('travel_journey')

class DeploymentGate(MiddlewareMixin):
    def process_view(self,request,view_func,view_args,view_kwargs):
        # Legacy manual import/edit/quote screens are for the site's maintainer.
        staff=getattr(getattr(request,'user',None),'is_staff',False)
        if (request.path.startswith('/collect/') or (request.method=='POST' and not request.path.startswith(('/journey/','/admin/')))) and not staff:
            return HttpResponse('此操作仅供网站管理员使用。请从旅行方案页面开始。',status=403)
        if request.method!='POST':return None
        if request.path in _STARTS:
            if not request.session.session_key:request.session.create()
            hour=datetime.now(timezone.utc).strftime('%Y%m%d%H')
            session_key='online-start:'+hour+':'+request.session.session_key
            global_key='online-start:'+hour+':all'
            with _rate_lock:
                own=cache.get(session_key,0);total=cache.get(global_key,0)
                if own>=getattr(settings,'TRAVEL_SESSION_STARTS_PER_HOUR',3) or total>=getattr(settings,'TRAVEL_GLOBAL_STARTS_PER_HOUR',15):
                    messages.info(request,'本时段的旅行查询次数已用完，请稍后再试。已生成的方案可以继续查看。')
                    return redirect('travel_journey')
                if not _single_query.acquire(blocking=False):return _busy(request)
                request._travel_query_claimed=True
                cache.set(session_key,own+1,3700);cache.set(global_key,total+1,3700)
        elif request.path in _COSTLY:
            if not _single_query.acquire(blocking=False):return _busy(request)
            request._travel_query_claimed=True
        return None
    def process_response(self,request,response):
        if getattr(request,'_travel_query_claimed',False):
            request._travel_query_claimed=False;_single_query.release()
        if request.path.startswith(('/journey/','/admin/')):response['Cache-Control']='private, no-store'
        return response
