from django.contrib import admin
from .models import AmapRecord


@admin.register(AmapRecord)
class AmapRecordAdmin(admin.ModelAdmin):
    list_display = ["name", "attraction", "city", "rating", "fetched_at"]
    search_fields = ["name", "poi_id", "city", "address"]
    readonly_fields = [f.name for f in AmapRecord._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
