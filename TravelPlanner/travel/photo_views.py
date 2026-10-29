"""照片端点只允许读取当前会话行程内的景点，不接受外部URL。"""
import copy
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .journey_views import get_job
from .models import AmapRecord
from .photo_service import resolve_photo,photo_key

@require_GET
def scenic_photo(request,day,stop):
    item=get_job(request)
    try:
        if not item or not item.payload.get('done'):raise IndexError
        row=copy.deepcopy(item.payload['plan']['days'][day]['stops'][stop])
    except (KeyError,IndexError,TypeError):return JsonResponse({'error':'请先生成行程。'},status=404)
    key=photo_key(row)
    if request.GET.get('key')!=key:return JsonResponse({'error':'行程已变化，请刷新页面。'},status=409)
    record=AmapRecord.objects.filter(poi_id=row.get('poi_id','')).first()
    if record:
        row['photos']=row.get('photos') or record.photos
        for field in ('longitude','latitude'):
            if row.get(field) is None:row[field]=getattr(record,field)
    try:result=resolve_photo(row)
    except Exception:result={'missing':True,'message':'照片暂时未能取得，旅行方案不受影响。'}
    result=dict(result)
    if result.get('data'):result['url']='data:'+result.pop('mime')+';base64,'+result.pop('data')
    response=JsonResponse(result)
    response['Cache-Control']='private, no-store'
    return response
