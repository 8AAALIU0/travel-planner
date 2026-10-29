"""景点数据及来源，价格未知时保留为空，不能当作免费。"""
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class Attraction(models.Model):
    name = models.CharField("景点名称", max_length=150)
    city = models.CharField("城市", max_length=60, db_index=True)
    tag = models.CharField("主题", max_length=60, blank=True)
    description = models.TextField("介绍", blank=True)
    style = models.CharField("插画样式", max_length=20, default="palace", choices=[
        ("palace", "建筑"), ("lake", "湖泊"), ("temple", "寺庙"), ("forest", "森林")])
    ticket_price = models.DecimalField("参考门票（元）", max_digits=8, decimal_places=2,
                                     null=True, blank=True, validators=[MinValueValidator(0)])
    latitude = models.FloatField("纬度", null=True, blank=True,
                                validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField("经度", null=True, blank=True,
                                 validators=[MinValueValidator(-180), MaxValueValidator(180)])
    source_url = models.URLField("资料来源网址", blank=True, max_length=600)
    is_demo = models.BooleanField("演示记录", default=True)
    updated_at = models.DateTimeField("修改时间", auto_now=True)

    # TRAVEL_COLLECT_FIELDS_V4
    ticket_type = models.CharField("参考票种 / 适用人群", max_length=120, blank=True, help_text="如成人普通票；不要将套票起价写成所有人的门票。")
    ticket_source_url = models.URLField("票价资料来源网址", max_length=600, blank=True)
    ticket_checked_on = models.DateField("票价核验日期", null=True, blank=True)
    ticket_notes = models.TextField("票价说明", blank=True, help_text="填写旺淡季、优惠条件或日期限制；价格未知时留空。")
    visit_minutes = models.PositiveIntegerField("预计游玩时长（分钟）", null=True, blank=True, validators=[MinValueValidator(15), MaxValueValidator(1440)], help_text="用于后续行程规划的估计值，并非高德实时数据。")
    visit_source_url = models.URLField("游玩时长参考来源", max_length=600, blank=True)


    class Meta:
        verbose_name = "旅游景点"
        verbose_name_plural = "旅游景点"
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["name", "city"], name="unique_attraction_city")]

    def __str__(self):
        return f"{self.city} · {self.name}"



# TRAVEL_AMAP_V3
class AmapRecord(models.Model):
    """确认匹配后保存的一次高德查询结果，与手工景点资料分开维护。"""
    attraction = models.OneToOneField(Attraction, on_delete=models.CASCADE, related_name="amap_record", verbose_name="关联景点")
    poi_id = models.CharField("高德 POI 编号", max_length=64, unique=True)
    name = models.CharField("平台地点名称", max_length=300)
    city = models.CharField("平台城市", max_length=100, blank=True)
    district = models.CharField("区县", max_length=100, blank=True)
    address = models.CharField("地址", max_length=1000, blank=True)
    category = models.CharField("地点分类", max_length=500, blank=True)
    rating = models.DecimalField("高德评分（5分制）", max_digits=3, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(5)])
    average_cost = models.DecimalField("人均消费（不是门票）", max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    longitude = models.FloatField("GCJ-02 经度", null=True, blank=True, validators=[MinValueValidator(-180), MaxValueValidator(180)])
    latitude = models.FloatField("GCJ-02 纬度", null=True, blank=True, validators=[MinValueValidator(-90), MaxValueValidator(90)])
    phone = models.CharField("电话", max_length=300, blank=True)
    hours_today = models.TextField("今日营业时间（获取当日）", blank=True)
    hours_week = models.TextField("营业时间说明", blank=True)
    photos = models.JSONField("平台图片网址", default=list, blank=True)
    fetched_at = models.DateTimeField("接口获取时间")

    class Meta:
        verbose_name = "高德景点资料"
        verbose_name_plural = "高德景点资料"

    def __str__(self):
        return self.name + " · " + self.poi_id



# TRAVEL_PLANNER_MODEL_V5
class PlannerCache(models.Model):
    """共享接口缓存，不保存用户预算或 API Key。"""
    cache_key = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "规划接口缓存"
        verbose_name_plural = "规划接口缓存"
