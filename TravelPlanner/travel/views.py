"""数据由 Django ORM 查询，所有样例和未知信息在页面中明确标识。"""
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from .models import Attraction


def home(request):
    query = request.GET.get("q", "").strip()[:100]
    city = request.GET.get("city", "").strip()[:60]
    cities = list(Attraction.objects.order_by("city").values_list("city", flat=True).distinct())
    if city not in cities:
        city = ""
    spots = Attraction.objects.all()
    if city:
        spots = spots.filter(city=city)
    if query:
        spots = spots.filter(Q(name__icontains=query) | Q(city__icontains=query) | Q(tag__icontains=query))
    total = Attraction.objects.count()
    return render(request, "travel/home.html", {
        "spots": spots, "cities": cities, "selected_city": city, "query": query,
        "total": total, "city_count": len(cities),
        "demo_count": Attraction.objects.filter(is_demo=True).count(),
    })


def detail(request, pk):
    spot = get_object_or_404(Attraction, pk=pk)
    return render(request, "travel/detail.html", {"spot": spot})
