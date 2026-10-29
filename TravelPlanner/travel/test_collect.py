"""网页采集的回归测试。运行 python manage.py test travel，不请求真实接口。"""
from decimal import Decimal
from io import StringIO
import json
import time
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, Client
from django.utils import timezone
from .models import Attraction, AmapRecord
from .amap_client import AmapError
from .collect_service import preview_rows, import_rows
from .collect_views import PREVIEW
from .collect_api import search_page

POI = {"id": "TEST_WEST_LAKE", "name": "西湖风景名胜区", "cityname": "杭州市", "adname": "西湖区", "address": "杭州西湖", "type": "风景名胜;风景名胜", "location": "120.14,30.24", "business": {"rating": "4.9", "cost": "50"}, "photos": []}


def make_preview(pois=None):
    return {"rows": preview_rows(pois if pois is not None else [POI]), "city": "杭州市", "keyword": "景点", "page": 1,
        "fetched_at": timezone.now().isoformat(), "expires_at": time.time() + 1800}


class CollectTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("collector", password="test-password")
        self.client.force_login(self.user)

    def set_preview(self, preview=None):
        session = self.client.session
        session[PREVIEW] = preview or make_preview()
        session.save()

    def test_staff_only_and_csrf(self):
        anonymous = Client()
        with patch("travel.collect_views.search_page") as api:
            self.assertEqual(anonymous.get("/collect/").status_code, 302)
            self.assertEqual(anonymous.post("/collect/", {"action": "search"}).status_code, 302)
            api.assert_not_called()
        plain = get_user_model().objects.create_user("plain", password="test-password", is_staff=True)
        self.client.force_login(plain)
        self.assertEqual(self.client.get("/collect/").status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.get("/collect/").status_code, 200)
        self.assertEqual(csrf_client.post("/collect/", {"action": "import"}).status_code, 403)

    @patch.dict("os.environ", {"TRAVEL_AMAP_KEY": "a" * 32})
    def test_search_preview_no_credential_no_database_write(self):
        with patch("travel.collect_views.search_page", return_value=[POI]) as api:
            response = self.client.post("/collect/", {"action": "search", "city": "杭州市", "keyword": "景点", "page": "2"}, follow=True)
            self.assertEqual(response.status_code, 200)
            api.assert_called_once_with("a" * 32, "杭州市", "景点", 2)
            self.assertContains(response, "西湖风景名胜区")
            self.assertNotContains(response, "a" * 32)
            self.assertNotIn("a" * 32, json.dumps(dict(self.client.session)))
            self.assertEqual(Attraction.objects.count(), 0)
            self.client.get("/collect/")
            api.assert_called_once()

    def test_import_selected_only_and_no_more_requests(self):
        self.set_preview(make_preview([POI, {**POI, "id": "OTHER", "name": "另一个景点"}]))
        with patch("travel.collect_views.search_page") as api:
            response = self.client.post("/collect/", {"action": "import", "poi_ids": [POI["id"]]}, follow=True)
            api.assert_not_called()
        self.assertContains(response, "新增 1 条")
        self.assertEqual(Attraction.objects.count(), 1)
        spot = Attraction.objects.get()
        self.assertEqual(spot.city, "杭州")
        self.assertFalse(spot.is_demo)
        self.assertIsNone(spot.ticket_price)
        self.assertIsNone(spot.visit_minutes)
        self.assertEqual(spot.ticket_source_url, "")
        self.assertEqual(spot.amap_record.average_cost, Decimal("50.00"))
        self.assertContains(self.client.get("/?city=杭州市"), "西湖风景名胜区")

    def test_repeat_update_preserves_manual_fields(self):
        spot = Attraction.objects.create(name=POI["name"], city="杭州", description="手工介绍", ticket_price=60, visit_minutes=180,
            ticket_type="成人普通票", ticket_source_url="https://example.com/ticket", ticket_checked_on="2026-10-25", is_demo=True)
        result = import_rows(make_preview(), [POI["id"]])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(Attraction.objects.count(), 1)
        result = import_rows(make_preview([{**POI, "business": {}}]), [POI["id"]])
        self.assertEqual(result["updated"], 1)
        spot.refresh_from_db()
        self.assertEqual(spot.description, "手工介绍")
        self.assertEqual(spot.ticket_price, 60)
        self.assertEqual(spot.visit_minutes, 180)
        self.assertEqual(spot.ticket_type, "成人普通票")
        self.assertTrue(spot.is_demo)
        self.assertIsNone(spot.amap_record.rating)
        response = self.client.get(f"/attractions/{spot.pk}/")
        self.assertContains(response, "180 分钟")
        self.assertContains(response, "2026-10-25")
        self.assertContains(response, "https://example.com/ticket")

    def test_missing_expired_or_foreign_preview_rejected(self):
        self.client.post("/collect/", {"action": "import", "poi_ids": [POI["id"]]})
        preview = make_preview()
        preview["expires_at"] = time.time() - 1
        self.set_preview(preview)
        response = self.client.post("/collect/", {"action": "import", "poi_ids": [POI["id"]]}, follow=True)
        self.assertContains(response, "已过期")
        self.set_preview()
        self.client.post("/collect/", {"action": "import", "poi_ids": ["FAKE"]})
        self.assertEqual(Attraction.objects.count(), 0)

    @patch.dict("os.environ", {"TRAVEL_AMAP_KEY": "a" * 32})
    def test_failed_search_drops_old_preview_and_no_mutation(self):
        self.set_preview()
        with patch("travel.collect_views.search_page", side_effect=AmapError("接口不可用")):
            response = self.client.post("/collect/", {"action": "search", "city": "北京市", "keyword": "博物馆", "page": 1}, follow=True)
        self.assertContains(response, "接口不可用")
        self.assertNotIn(PREVIEW, self.client.session)
        self.assertEqual(Attraction.objects.count(), 0)

    def test_conflicting_poi_mapping_and_invalid_row_no_orphan(self):
        import_rows(make_preview(), [POI["id"]])
        conflict = {**POI, "id": "DIFFERENT_ID"}
        result = import_rows(make_preview([conflict]), ["DIFFERENT_ID"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(AmapRecord.objects.count(), 1)
        invalid = {**POI, "id": "LONG_NAME", "name": "某" * 151}
        result = import_rows(make_preview([invalid]), ["LONG_NAME"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(Attraction.objects.count(), 1)

    def test_pagination_city_and_rating(self):
        for index in range(14):
            Attraction.objects.create(name=f"公园{index}", city="杭州", tag="公园")
        Attraction.objects.create(name="外地公园", city="北京", tag="公园")
        response = self.client.get("/?city=杭州市&tag=公园&sort=rating")
        self.assertEqual(response.context["result_count"], 14)
        self.assertEqual(len(response.context["spots"]), 12)
        self.assertNotContains(response, "外地公园")
        self.assertEqual(len(self.client.get("/?city=杭州&page=2").context["spots"]), 2)
        self.assertContains(self.client.get("/?city=不存在"), "暂时没有匹配的景点")

    def test_source_text_escaped_and_duration_validated(self):
        spot = Attraction(name="安全测试", city="北京", ticket_notes="<script>bad</script>", visit_minutes=0)
        with self.assertRaises(ValidationError):
            spot.full_clean()
        spot.visit_minutes = 120
        spot.save()
        response = self.client.get(f"/attractions/{spot.pk}/")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>bad</script>")

    def test_page_request_parameters_and_secret_redaction(self):
        with patch("travel.collect_api.urlopen") as api:
            api.return_value.__enter__.return_value.read.return_value = json.dumps({"status": "1", "pois": [POI]}).encode()
            self.assertEqual(search_page("b" * 32, "杭州", "景点", 3), [POI])
            params = parse_qs(urlsplit(api.call_args.args[0].full_url).query)
            self.assertEqual(params["page_num"], ["3"])
            self.assertEqual(params["page_size"], ["25"])
            self.assertEqual(params["city_limit"], ["true"])
            api.assert_called_once()
            api.return_value.__enter__.return_value.read.return_value = json.dumps({"status": "0", "info": "bad " + "b" * 32}).encode()
            with self.assertRaises(AmapError) as error:
                search_page("b" * 32, "杭州", "景点", 1)
            self.assertNotIn("b" * 32, str(error.exception))
        with self.assertRaises(AmapError):
            search_page("b" * 32, "杭州", "景点", 9)

    def test_launcher_key_is_memory_only(self):
        with patch.dict("os.environ", {}, clear=True), patch("travel.management.commands.runtravel.ask_key", return_value="c" * 32), patch("travel.management.commands.runtravel.call_command") as server:
            call_command("runtravel", stdout=StringIO())
            server.assert_called_once_with("runserver", addrport="127.0.0.1:8000", use_reloader=False)
