"""先校验全部 CSV，再在同一事务中导入；错误不会留下部分数据。"""
import csv
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from travel.models import Attraction


class Command(BaseCommand):
    help = "导入景点 CSV：同名同城更新，重复执行不会新增重复记录"

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path)

    def handle(self, *args, **options):
        rows = []
        seen = set()
        try:
            with options["csv_path"].open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or not {"name", "city", "is_demo"}.issubset(reader.fieldnames):
                    raise ValueError("CSV 必须包含 name、city、is_demo 三列")
                for line, raw in enumerate(reader, 2):
                    row = {k: (v or "").strip() for k, v in raw.items() if k}
                    if None in raw or any(v is None for v in raw.values()):
                        raise ValueError(f"第 {line} 行列数不正确")
                    flag = row["is_demo"].lower()
                    if flag not in {"true", "false", "1", "0"}:
                        raise ValueError(f"第 {line} 行 is_demo 必须是 true/false 或 1/0")
                    record = {key: row.get(key, "") for key in
                              ("name", "city", "tag", "description", "source_url")}
                    record["is_demo"] = flag in {"true", "1"}
                    record["style"] = row.get("style") or "palace"
                    price = row.get("ticket_price", "")
                    record["ticket_price"] = Decimal(price) if price else None
                    if record["ticket_price"] is not None and not record["ticket_price"].is_finite():
                        raise ValueError(f"第 {line} 行票价必须是有限数值")
                    for key in ("latitude", "longitude"):
                        value = row.get(key, "")
                        record[key] = float(value) if value else None
                        if record[key] is not None and not math.isfinite(record[key]):
                            raise ValueError(f"第 {line} 行坐标必须是有限数值")
                    key = record["name"], record["city"]
                    if key in seen:
                        raise ValueError(f"第 {line} 行出现同名同城重复景点")
                    seen.add(key)
                    Attraction(**record).full_clean(validate_unique=False, validate_constraints=False)
                    rows.append(record)
            if not rows:
                raise ValueError("CSV 没有可导入的数据")
        except Exception as error:
            raise CommandError(f"导入前校验失败，数据库未修改：{error}") from error
        created = updated = 0
        with transaction.atomic():
            for record in rows:
                name = record.pop("name")
                city = record.pop("city")
                _, is_new = Attraction.objects.update_or_create(name=name, city=city, defaults=record)
                created += int(is_new)
                updated += int(not is_new)
        self.stdout.write(self.style.SUCCESS(f"导入完成：新增 {created} 条，更新 {updated} 条。"))
