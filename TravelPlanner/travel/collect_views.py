import os
import time
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Q, F
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods
from .models import Attraction, AmapRecord
from .amap_client import AmapError
from .collect_api import search_page
from .collect_service import preview_rows, import_rows

PREVIEW = "travel_collect_preview_v4"


def catalog(request):
    query = request.GET.get("q", "").strip()[:100]
    city = request.GET.get("city", "").strip()[:60]
    tag = request.GET.get("tag", "").strip()[:60]
    sort = request.GET.get("sort", "latest")
    cities = list(Attraction.objects.order_by("city").values_list("city", flat=True).distinct())
    tags = list(Attraction.objects.exclude(tag="").order_by("tag").values_list("tag", flat=True).distinct())
    spots = Attraction.objects.select_related("amap_record")
    if city:
        normal_city = city[:-1] if city.endswith("市") else city
        spots = spots.filter(city__in=[normal_city, normal_city + "市"])
    if tag:
        spots = spots.filter(tag=tag)
    if query:
        spots = spots.filter(Q(name__icontains=query) | Q(city__icontains=query) | Q(tag__icontains=query) | Q(amap_record__address__icontains=query))
    if sort == "rating":
        spots = spots.order_by(F("amap_record__rating").desc(nulls_last=True), "id")
    elif sort == "name":
        spots = spots.order_by("name", "id")
    else:
        sort = "latest"
        spots = spots.order_by("-id")
    page = Paginator(spots, 12).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "travel/home.html", {"spots": page, "page_obj": page,
        "cities": cities, "tags": tags, "query": query, "selected_city": city, "selected_tag": tag, "sort": sort,
        "filter_query": params.urlencode(), "result_count": page.paginator.count,
        "total": Attraction.objects.count(), "city_count": len(cities),
        "demo_count": Attraction.objects.filter(is_demo=True).count(), "amap_count": AmapRecord.objects.count()})


@staff_member_required
@require_http_methods(["GET", "POST"])
@sensitive_variables("key")
def collect(request):
    if not request.user.has_perms(["travel.add_attraction", "travel.change_attraction", "travel.add_amaprecord", "travel.change_amaprecord"]):
        return HttpResponseForbidden("需要景点和高德资料的新增、修改权限。请使用原管理员账号登录。")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "search":
            # 清理旧候选，避免新搜索失败后误导入上一城市。
            request.session.pop(PREVIEW, None)
            city = request.POST.get("city", "").strip()
            keyword = request.POST.get("keyword", "").strip()
            try:
                page = int(request.POST.get("page", "1"))
                if not 1 <= page <= 8:
                    raise ValueError
            except ValueError:
                messages.error(request, "页码应为 1—8。")
                return redirect("travel_collect")
            request.session["collect_form_v4"] = {"city": city[:60], "keyword": keyword[:80], "page": page}
            if not city or len(city) > 60 or city in ("全国", "中国", "全部") or not keyword or len(keyword) > 80:
                messages.error(request, "请填写具体城市（如杭州市）和 1—80 字的关键词（如景点、博物馆）。")
                return redirect("travel_collect")
            key = os.environ.get("TRAVEL_AMAP_KEY", "")
            if not key:
                messages.error(request, "本次启动尚未设置高德 Key。请停止服务器，使用 manage.py runtravel 启动并在本机弹窗粘贴 Key。")
                return redirect("travel_collect")
            now = time.time()
            if now - request.session.get("collect_last_search_v4", 0) < 3:
                messages.error(request, "请间隔至少 3 秒再搜索，避免重复请求。")
                return redirect("travel_collect")
            request.session["collect_last_search_v4"] = now
            try:
                rows = preview_rows(search_page(key, city, keyword, page))
            except AmapError as error:
                messages.error(request, str(error))
            else:
                request.session[PREVIEW] = {"rows": rows, "city": city, "keyword": keyword, "page": page,
                    "fetched_at": timezone.now().isoformat(), "expires_at": now + 1800}
                messages.success(request, f"第 {page} 页返回 {len(rows)} 条可预览资料。请核对景点主体后勾选导入。" if rows else "本页没有结果，可换关键词或城市再查。")
        elif action == "import":
            preview = request.session.get(PREVIEW)
            if not preview or preview.get("expires_at", 0) <= time.time():
                messages.error(request, "搜索预览已过期（30 分钟），请重新搜索。")
            else:
                try:
                    report = import_rows(preview, request.POST.getlist("poi_ids"))
                except AmapError as error:
                    messages.error(request, str(error))
                else:
                    request.session["collect_report_v4"] = report
                    messages.success(request, f"导入完成：新增 {report['added']} 条，更新高德资料 {report['updated']} 条，跳过 {report['skipped']} 条。")
        else:
            messages.error(request, "无法识别这个操作。")
        return redirect("travel_collect")
    preview = request.session.get(PREVIEW)
    if preview and preview.get("expires_at", 0) <= time.time():
        request.session.pop(PREVIEW, None)
        preview = None
    if preview:
        linked = set(AmapRecord.objects.filter(poi_id__in=[r["poi_id"] for r in preview["rows"]]).values_list("poi_id", flat=True))
        for row in preview["rows"]:
            row["exists"] = row["poi_id"] in linked
    return render(request, "travel/collect.html", {
        "preview": preview, "form_values": request.session.get("collect_form_v4", {"city": "杭州市", "keyword": "景点", "page": 1}),
        "key_ready": bool(os.environ.get("TRAVEL_AMAP_KEY")), "report": request.session.pop("collect_report_v4", None),
        "total": Attraction.objects.count(), "amap_count": AmapRecord.objects.count()})
