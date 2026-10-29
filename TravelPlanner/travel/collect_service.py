"""使用服务端预览中的数据导入，按 POI 去重；保留已有手工资料。"""
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .amap_client import AmapError, normalize
from .models import Attraction, AmapRecord


def canonical_city(value):
    value = value.strip()
    return value[:-1] if value.endswith("市") else value


def preview_rows(pois):
    rows, seen = [], set()
    for poi in pois:
        try:
            values = normalize(poi)
        except AmapError:
            continue
        if values["poi_id"] in seen:
            continue
        seen.add(values["poi_id"])
        # JSON session 不存储 Decimal。空值保持 null。
        for field in ("rating", "average_cost"):
            if values[field] is not None:
                values[field] = str(values[field])
        values["suspect"] = any(word in values["name"] for word in ("停车场", "售票", "检票", "入口", "出口", "出入口", "游客中心", "卫生间"))
        rows.append(values)
    return rows


def import_rows(preview, selected):
    rows = {row["poi_id"]: row for row in preview["rows"]}
    if not selected or len(selected) > 25 or not set(selected) <= rows.keys():
        raise AmapError("请勾选本次搜索结果中的景点；不要使用已过期的页面。")
    fetched = parse_datetime(preview["fetched_at"])
    if not fetched or not timezone.is_aware(fetched):
        raise AmapError("预览获取时间异常，请重新搜索。")
    added, updated, skipped, failures = 0, 0, 0, []
    # 各行独立事务，单行冲突明确报告，不阻断其他已选景点。
    for poi_id in dict.fromkeys(selected):
        values = {k: v for k, v in rows[poi_id].items() if k not in ("suspect", "exists")}
        try:
            with transaction.atomic():
                record = AmapRecord.objects.select_related("attraction").filter(poi_id=poi_id).first()
                if record:
                    spot = record.attraction
                    is_new = False
                else:
                    city = canonical_city(values["city"])
                    if not city:
                        raise AmapError("平台未返回城市，请先核对地点后手工录入。")
                    name = values["name"]
                    if len(name) > 150 or len(city) > 60:
                        raise AmapError("平台名称或城市过长，未自动截断导入。")
                    matches = Attraction.objects.filter(name=name, city__in=[city, city + "市"])
                    if matches.count() > 1:
                        raise AmapError("存在同名城市记录，请先在后台合并核对。")
                    spot = matches.first()
                    is_new = spot is None
                    if spot and AmapRecord.objects.filter(attraction=spot).exists():
                        raise AmapError("已有同名景点绑定另一个 POI，请先核对。")
                    if is_new:
                        category = values["category"]
                        tag = "博物馆" if "博物馆" in category else "公园" if "公园" in category else "风景名胜" if "风景名胜" in category else "待分类"
                        spot = Attraction(name=name, city=city, tag=tag, is_demo=False, description="")
                        spot.full_clean()
                        spot.save()
                    record = AmapRecord(attraction=spot)
                for field, value in values.items():
                    setattr(record, field, value)
                record.fetched_at = fetched
                record.full_clean()
                record.save()
            if is_new:
                added += 1
            else:
                updated += 1
        except (AmapError, ValidationError, IntegrityError) as error:
            skipped += 1
            message = str(error) if isinstance(error, AmapError) else "字段校验或唯一性冲突，请在后台核对。"
            failures.append(values["name"] + "：" + message)
    return {"added": added, "updated": updated, "skipped": skipped, "failures": failures}
