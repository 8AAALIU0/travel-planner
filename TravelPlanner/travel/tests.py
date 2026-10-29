"""运行：python manage.py test travel。使用临时测试数据库。"""
import tempfile
from pathlib import Path
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from .models import Attraction


class AttractionTests(TestCase):
    def setUp(self):
        self.spot = Attraction.objects.create(name="测试博物馆", city="北京", tag="历史人文")
        Attraction.objects.create(name="测试湖", city="杭州", tag="自然风光")

    def test_combined_filter(self):
        response = self.client.get(reverse("travel_home"), {"q": "博物馆", "city": "北京"})
        self.assertContains(response, "测试博物馆")
        self.assertNotContains(response, "测试湖")

    def test_unknown_price_is_not_zero(self):
        url = reverse("attraction_detail", args=[self.spot.pk])
        self.assertContains(self.client.get(url), "暂无资料")
        self.spot.ticket_price = 0
        self.spot.save()
        self.assertContains(self.client.get(url), "¥ 0.00")

    def test_detail_escapes_content(self):
        self.spot.description = "<script>alert(1)</script>"
        self.spot.save()
        response = self.client.get(reverse("attraction_detail", args=[self.spot.pk]))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_missing_record_is_404(self):
        self.assertEqual(self.client.get(reverse("attraction_detail", args=[999999])).status_code, 404)

    def test_import_validation_and_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.csv"
            path.write_text("name,city,is_demo,ticket_price\n新景点,北京,true,10\n错误景点,北京,true,-1\n", encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("import_attractions", path, verbosity=0)
            self.assertEqual(Attraction.objects.count(), 2)
            path.write_text("name,city,is_demo\n新景点,北京,true\n", encoding="utf-8")
            call_command("import_attractions", path, verbosity=0)
            call_command("import_attractions", path, verbosity=0)
            self.assertEqual(Attraction.objects.count(), 3)
