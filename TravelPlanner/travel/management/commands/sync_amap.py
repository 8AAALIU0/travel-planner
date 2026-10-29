"""显式选择匹配地点后落库。执行一次只请求一页，不自动循环抓取。"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone
from travel.models import Attraction, AmapRecord
from travel.amap_client import AmapError, ask_key, search, normalize, text_value


class Command(BaseCommand):
    help = "查询高德并选择一个 POI，保存该景点的高德资料（保留手工介绍与门票）。"

    def add_arguments(self, parser):
        parser.add_argument("--name", default="故宫博物院", help="本地数据库里的景点名称")
        parser.add_argument("--city", default="北京", help="本地数据库里的城市名称")

    def handle(self, *args, **options):
        try:
            return self.run_import(options)
        except (KeyboardInterrupt, EOFError):
            self.stdout.write("已取消，未保存本次查询。")
        except AmapError as error:
            raise CommandError(str(error)) from None
        except ValidationError:
            raise CommandError("候选数据未通过字段校验，未修改数据库。") from None
        except IntegrityError:
            raise CommandError("该 POI 已关联其他景点，或存在数据冲突。未修改本次数据。") from None

    def run_import(self, options):
        name, city = options["name"].strip(), options["city"].strip()
        if not name or len(name) > 80 or not city or len(city) > 60:
            raise CommandError("景点名称需 1—80 字，城市需 1—60 字。")
        try:
            spot = Attraction.objects.get(name=name, city=city)
        except Attraction.DoesNotExist:
            raise CommandError("数据库中没有这个名称与城市的景点，请先在后台添加或核对名称。") from None
        self.stdout.write(f"将查询：{spot.city} / {spot.name}。一次请求，最多 5 条候选。")
        key = ask_key()
        if key is None:
            self.stdout.write("已取消，没有发送请求。")
            return
        pois = search(key, name, city)
        fetched_at = timezone.now()
        key = None
        if not pois:
            self.stdout.write("没有匹配结果，未修改数据库。")
            return
        for index, poi in enumerate(pois, 1):
            self.stdout.write(f"\n{index}. {text_value(poi.get('name'))} | {text_value(poi.get('id'))}")
            self.stdout.write("   城市/区县：" + text_value(poi.get("cityname")) + " / " + text_value(poi.get("adname")))
            self.stdout.write("   地址：" + text_value(poi.get("address")))
            self.stdout.write("   分类：" + text_value(poi.get("type")))
        self.stdout.write("\n请核对景点主体；不要选午门、停车场或售票处。输入编号后按回车；输入 0 取消。")
        while True:
            choice = input("候选编号：").strip()
            if choice == "0":
                self.stdout.write("已取消，未修改数据库。")
                return
            if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(pois):
                break
            self.stdout.write("请输入列表中的编号，或输入 0 取消。")
        values = normalize(pois[int(choice) - 1])
        values["fetched_at"] = fetched_at
        with transaction.atomic():
            # 已有关联不允许悄悄改成另一个地点。
            Attraction.objects.select_for_update().get(pk=spot.pk)
            record = AmapRecord.objects.filter(attraction=spot).first()
            if record and record.poi_id != values["poi_id"]:
                raise AmapError("该景点已绑定另一个 POI。请核对地点；若需重绑，在后台删除原高德资料后再导入。")
            record = record or AmapRecord(attraction=spot)
            for field, value in values.items():
                setattr(record, field, value)
            record.full_clean()
            record.save()
        self.stdout.write(self.style.SUCCESS(f"导入成功：{record.name}；高德 POI {record.poi_id}"))
        self.stdout.write(f"打开 http://127.0.0.1:8000/attractions/{spot.pk}/ 查看。")
        self.stdout.write("原介绍、手工门票和演示标记均保留。再次导入会以本次完整查询结果更新高德资料，缺失项显示未知。")
