"""Keep access and concurrent-work guards without per-hour query quotas."""
import threading

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin


_single_query = threading.BoundedSemaphore(1)
_COSTLY = {
    '/journey/',
    '/journey/retry-transport/',
    '/journey/transport-preferences/',
    '/journey/retry-hotels/',
    '/journey/select-transport/',
    '/journey/step/',
    '/journey/optimize/',
}


def _busy(request):
    if request.path == '/journey/step/':
        return JsonResponse({'running': True, 'queued': True})
    messages.info(request, '网站正在处理其他旅行查询，请稍后再试。已有方案仍保留。')
    return redirect('travel_journey')


class DeploymentGate(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        # Manual import and editing screens remain restricted to the maintainer.
        staff = getattr(getattr(request, 'user', None), 'is_staff', False)
        if (
            request.path.startswith('/collect/')
            or (
                request.method == 'POST'
                and not request.path.startswith(('/journey/', '/admin/'))
            )
        ) and not staff:
            return HttpResponse('此操作仅供网站管理员使用。请从旅行方案页面开始。', status=403)

        # No hourly session quota, shared quota, or query-start counter.
        if request.method == 'POST' and request.path in _COSTLY:
            if not _single_query.acquire(blocking=False):
                return _busy(request)
            request._travel_query_claimed = True
        return None

    def process_response(self, request, response):
        if getattr(request, '_travel_query_claimed', False):
            request._travel_query_claimed = False
            _single_query.release()
        if request.path.startswith(('/journey/', '/admin/')):
            response['Cache-Control'] = 'private, no-store'
        return response
