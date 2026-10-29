
from io import StringIO
from unittest.mock import patch
from django.test import TestCase, Client
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from decimal import Decimal
from travel.models import Attraction, AmapRecord
from travel.amap_client import normalize, AmapError, search
import json

POI = {"id":"B000A8UIN8","name":"故宫博物院","cityname":"北京市","adname":"东城区","address":"景山前街4号","type":"风景名胜;世界遗产","location":"116.397029,39.917839","business":{"rating":"4.9","cost":[],"opentime_week":"周一闭馆"},"photos":[{"url":"https://example.com/palace.jpg"},{"url":"javascript:bad"}]}

class AmapIntegrationTests(TestCase):
    def setUp(self):
        self.spot=Attraction.objects.create(name="故宫博物院",city="北京",description="我自己的介绍 <script>bad</script>",ticket_price=60,is_demo=True)

    def sync(self, pois=None, choice="1"):
        with patch("travel.management.commands.sync_amap.ask_key",return_value="a"*32), patch("travel.management.commands.sync_amap.search",return_value=pois if pois is not None else [POI]) as api, patch("builtins.input",return_value=choice):
            call_command("sync_amap",stdout=StringIO())
            return api.call_count

    def test_select_persist_preserve_and_render(self):
        self.assertEqual(self.sync(),1)
        self.spot.refresh_from_db()
        self.assertEqual(self.spot.description,"我自己的介绍 <script>bad</script>")
        self.assertEqual(self.spot.ticket_price,Decimal("60.00"))
        self.assertTrue(self.spot.is_demo)
        record=self.spot.amap_record
        self.assertEqual(record.rating,Decimal("4.90"))
        self.assertIsNone(record.average_cost)
        self.assertEqual(record.photos,["https://example.com/palace.jpg"])
        self.assertAlmostEqual(record.longitude,116.397029)
        response=Client().get(f"/attractions/{self.spot.pk}/")
        self.assertEqual(response.status_code,200)
        self.assertContains(response,"4.9")
        self.assertContains(response,"景山前街4号")
        self.assertContains(response,"&lt;script&gt;")
        self.assertNotContains(response,"<script>bad</script>")
        self.assertNotContains(response,"a"*32)
        self.assertContains(response,"GCJ-02")

    def test_refresh_clears_missing_and_remap_refused(self):
        self.sync()
        self.sync([{**POI,"business":{}}])
        self.assertEqual(AmapRecord.objects.count(),1)
        self.assertIsNone(AmapRecord.objects.get().rating)
        with self.assertRaises(CommandError):
            self.sync([{**POI,"id":"OTHER"}])
        self.assertEqual(AmapRecord.objects.get().poi_id,POI["id"])

    def test_cancels_and_empty_result(self):
        self.sync(choice="0")
        self.sync([])
        self.assertFalse(AmapRecord.objects.exists())
        with patch("travel.management.commands.sync_amap.ask_key",return_value=None), patch("travel.management.commands.sync_amap.search") as api:
            call_command("sync_amap",stdout=StringIO())
            api.assert_not_called()

    def test_conflict_rolls_back(self):
        other=Attraction.objects.create(name="另一景点",city="北京")
        AmapRecord.objects.create(attraction=other,**normalize(POI),fetched_at=timezone.now())
        with self.assertRaises(CommandError):
            self.sync()
        self.assertEqual(AmapRecord.objects.count(),1)
        self.assertFalse(AmapRecord.objects.filter(attraction=self.spot).exists())

    def test_unknown_zero_invalid_and_no_snapshot(self):
        fields=normalize({**POI,"location":"NaN,39","business":{"rating":"0","cost":"0"}})
        self.assertEqual(fields["rating"],0)
        self.assertEqual(fields["average_cost"],0)
        self.assertIsNone(fields["longitude"])
        fields=normalize({**POI,"business":{"rating":"NaN","cost":"-1"},"location":"999,40"})
        self.assertIsNone(fields["rating"])
        self.assertIsNone(fields["average_cost"])
        response=Client().get(f"/attractions/{self.spot.pk}/")
        self.assertContains(response,"尚未关联高德资料")

    def test_api_one_request_and_redaction(self):
        with patch("travel.amap_client.urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value=json.dumps({"status":"1","pois":[POI]}).encode()
            self.assertEqual(search("a"*32,"故宫博物院","北京"),[POI])
            request.assert_called_once()
            request.return_value.__enter__.return_value.read.return_value=json.dumps({"status":"0","info":"bad "+"a"*32}).encode()
            with self.assertRaises(AmapError) as error:
                search("a"*32,"故宫博物院","北京")
            self.assertNotIn("a"*32,str(error.exception))
