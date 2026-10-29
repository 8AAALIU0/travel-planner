from django.contrib import admin
from .models import Attraction


@admin.register(Attraction)
class AttractionAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "tag", "ticket_price", "is_demo", "updated_at"]
    list_filter = ["city", "tag", "is_demo"]
    search_fields = ["name", "city", "tag", "description"]
    readonly_fields = ["updated_at"]
    list_per_page = 25


# TRAVEL_AMAP_V3
from . import amap_admin
