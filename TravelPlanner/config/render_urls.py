from .urls import urlpatterns
from django.urls import path
from django.http import JsonResponse
from django.db import connection

def health(request):
    try:
        with connection.cursor() as cursor:cursor.execute('SELECT 1');cursor.fetchone()
        return JsonResponse({'status':'ok'})
    except Exception:return JsonResponse({'status':'unavailable'},status=503)
urlpatterns=[path('healthz/',health,name='travel_health')]+urlpatterns
