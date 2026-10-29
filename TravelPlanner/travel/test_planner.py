"""自动规划的预算、自动获取、缓存、失败降级及页面回归测试。"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.utils import timezone
from .models import Attraction, AmapRecord, PlannerCache
from .planner_forms import PlannerForm
from .planner_service import build_plan, fetch_pool, enrich, safe_candidate
from .planner_api import route_between, request_json, read_cache, write_cache
from .amap_client import AmapError


def preferences(**overrides):
    result=dict(city="杭州市",start_date=timezone.localdate()+timedelta(days=1),days=3,people=2,budget=Decimal("2000"),
        interests=["nature","culture"],pace="relaxed",mode="transit",hotel=Decimal("300"),rooms=1,food=Decimal("80"),
        transport=Decimal("30"),unknown_ticket=Decimal("50"),intercity=Decimal("0"))
    result.update(overrides)
    return result


def rows(count=9):
    return [dict(poi_id=f"P{i}",name=f"示例公园{i}",city="杭州市",district="西湖区",address=f"测试地址{i}",category="风景名胜;公园",
        longitude=120.1+i*.003,latitude=30.2,rating="4.8",average_cost=None,phone="",hours_today="",hours_week="09:00-17:00",photos=[],
        fetched_at=timezone.now().isoformat()) for i in range(count)]


def pois():
    return [dict(id="REAL_A",name="测试自然公园",cityname="杭州市",adname="西湖区",address="测试地址",type="风景名胜;公园",location="120.1,30.2",business={"rating":"4.9"}),
        dict(id="PARKING",name="测试自然公园停车场",cityname="杭州市",type="风景名胜",location="120.11,30.2",business={}),
        dict(id="OTHER_CITY",name="异地公园",cityname="北京市",type="风景名胜;公园",location="116.1,39.2",business={})]


class PlannerTests(TestCase):
    def plan(self,p=None,pool=None,route=None):
        with patch("travel.planner_service.fetch_pool",return_value=(pool if pool is not None else rows(),[],0)),patch("travel.planner_service.route_between",return_value=route or {"minutes":12,"km":1.2,"verified":True,"summary":"测试公交","fetched_at":timezone.now().isoformat()}):
            return build_plan(p or preferences(),"a"*32)

    def test_budget_constraint_unknown_not_free_no_duplicate(self):
        result=self.plan()
        self.assertLessEqual(Decimal(result["total"]),Decimal(result["budget"]))
        self.assertGreater(result["unknown_tickets"],0)
        stops=[s for d in result["days"] for s in d["stops"]]
        self.assertGreater(len(stops),0)
        self.assertEqual(len(stops),len({s["poi_id"] for s in stops}))
        self.assertTrue(all(Decimal(s["ticket_per_person"])==50 for s in stops))
        self.assertEqual(Decimal(result["costs"]["hotel"]),600)
        self.assertEqual(Decimal(result["costs"]["food"]),480)
        self.assertEqual(Decimal(result["total"]),sum(Decimal(v) for v in result["costs"].values()))

    def test_unaffordable_base_no_api_calls(self):
        with patch("travel.planner_service.fetch_pool") as api:
            result=build_plan(preferences(budget=Decimal(100)),"a"*32)
        api.assert_not_called()
        self.assertEqual(result["status"],"over_budget")

    def test_automatic_query_save_filter_and_cache(self):
        with patch("travel.planner_service.search_places",return_value=pois()) as api:
            found,_,_=fetch_pool("杭州市",["nature","culture"],"a"*32)
            self.assertEqual(api.call_count,2)
            self.assertEqual(len(found),1)
            self.assertEqual(AmapRecord.objects.count(),1)
            self.assertEqual(Attraction.objects.get().name,"测试自然公园")
            self.assertIsNone(Attraction.objects.get().ticket_price)
            fetch_pool("杭州市",["nature","culture"],"a"*32)
            self.assertEqual(api.call_count,2)
            self.assertEqual(AmapRecord.objects.count(),1)
            self.assertEqual(PlannerCache.objects.count(),2)

    def test_network_failure_uses_local_and_preserves_manual(self):
        spot=Attraction.objects.create(name="手工公园",city="杭州",description="保留手工说明",ticket_price=90,visit_minutes=180)
        AmapRecord.objects.create(attraction=spot,poi_id="LOCAL",name="手工公园",city="杭州市",category="风景名胜;公园",longitude=120.1,latitude=30.2,fetched_at=timezone.now())
        with patch("travel.planner_service.search_places",side_effect=AmapError("网络失败")):
            found,warnings,_=fetch_pool("杭州",["nature"],"a"*32)
        self.assertEqual(len(found),1)
        self.assertTrue(warnings)
        spot.refresh_from_db()
        self.assertEqual(spot.description,"保留手工说明")
        self.assertEqual(spot.ticket_price,90)

    def test_no_key_new_city_not_faked(self):
        with patch("travel.planner_service.search_places") as api:
            found,warnings,_=fetch_pool("杭州市",["nature"],"")
        api.assert_not_called()
        self.assertFalse(found)
        self.assertTrue(warnings)

    def test_ticket_reference_requires_source_and_recent_verification(self):
        spot=Attraction.objects.create(name="示例公园0",city="杭州",ticket_price=0,is_demo=False)
        AmapRecord.objects.create(attraction=spot,poi_id="P0",name=spot.name,city="杭州市",fetched_at=timezone.now())
        candidate=enrich(rows(1),preferences())[0]
        self.assertFalse(candidate["price_reference"])
        self.assertEqual(candidate["ticket_per_person"],"50")
        spot.ticket_source_url="https://example.com/ticket"
        spot.ticket_type="成人普通票"
        spot.ticket_checked_on=timezone.localdate()
        spot.save()
        candidate=enrich(rows(1),preferences())[0]
        self.assertTrue(candidate["price_reference"])
        self.assertEqual(Decimal(candidate["ticket_per_person"]),0)

    def test_route_failure_stays_explicitly_unverified(self):
        with patch("travel.planner_service.fetch_pool",return_value=(rows(),[],0)),patch("travel.planner_service.route_between",side_effect=AmapError("路线权限不足")):
            result=build_plan(preferences(),"a"*32)
        self.assertGreater(result["unverified_legs"],0)
        self.assertEqual(result["verified_legs"],0)
        self.assertTrue(any("未核验" in w for w in result["warnings"]))

    def test_excessive_actual_route_drops_later_stops(self):
        result=self.plan(route={"minutes":180,"km":50,"verified":True,"summary":"","fetched_at":timezone.now().isoformat()})
        self.assertTrue(all(len(d["stops"])<=1 for d in result["days"]))
        self.assertTrue(any("缩减" in w for w in result["warnings"]))

    def test_monday_closed_and_explicit_window(self):
        day=timezone.localdate()
        while day.weekday()!=0:
            day+=timedelta(days=1)
        pool=rows(1)
        pool[0]["hours_week"]="周一全天关闭"
        result=self.plan(preferences(days=1,start_date=day),pool=pool)
        self.assertEqual(result["stop_count"],0)
        pool[0]["hours_week"]="16:30-17:00"
        result=self.plan(preferences(days=1),pool=pool)
        self.assertEqual(result["stop_count"],0)

    def test_invalid_form_no_call(self):
        data={k:str(v) if not isinstance(v,list) else v for k,v in preferences().items()}
        data.update(days="0",people="20",unknown_ticket="0",budget="NaN")
        form=PlannerForm(data)
        self.assertFalse(form.is_valid())
        with patch("travel.planner_views.build_plan") as generator:
            response=self.client.post("/plan/",data)
        generator.assert_not_called()
        self.assertEqual(response.status_code,200)

    def test_public_form_result_refresh_export_and_csrf(self):
        data={k:str(v) if not isinstance(v,list) else v for k,v in preferences().items()}
        result=self.plan()
        with patch("travel.planner_views.build_plan",return_value=result) as generator:
            response=self.client.post("/plan/",data,follow=True)
            self.assertContains(response,"旅行方案")
            self.assertContains(response,"未知门票预留")
            self.client.get("/plan/")
            generator.assert_called_once()
            download=self.client.get("/plan/export/")
            self.assertEqual(download.status_code,200)
            self.assertEqual(download.json()["city"],"杭州市")
            self.assertNotContains(response,"a"*32)
            self.assertNotContains(download,"a"*32)
            self.client.post("/plan/",data)
            generator.assert_called_once()
        self.assertEqual(Client().get("/plan/export/").status_code,404)
        csrf=Client(enforce_csrf_checks=True)
        self.assertEqual(csrf.post("/plan/",data).status_code,403)

    def test_escape_upstream_text(self):
        pool=rows(1)
        pool[0]["name"]="<script>bad</script>公园"
        result=self.plan(pool=pool)
        session=self.client.session
        session["travel_plan_result_v5"]=result
        session.save()
        response=self.client.get("/plan/")
        self.assertContains(response,"&lt;script&gt;")
        self.assertNotContains(response,"<script>bad</script>")

    def test_route_parser_units_cache_and_errors(self):
        start,end=rows(2)
        with patch("travel.planner_api.request_json",return_value={"route":{"paths":[{"duration":"601","distance":"1500"}]}}) as api:
            result=route_between("a"*32,start,end,"walking","杭州")
            self.assertEqual(result["minutes"],11)
            self.assertEqual(result["km"],1.5)
            route_between("a"*32,start,end,"walking","杭州")
            api.assert_called_once()
        with patch("travel.planner_api.request_json",return_value={"route":{"transits":[{"duration":"NaN","distance":"1"}]}}):
            with self.assertRaises(AmapError):
                route_between("a"*32,start,end,"transit","杭州")

    def test_api_fixed_host_direct_and_key_redacted(self):
        with patch("travel.planner_api.build_opener") as builder:
            builder.return_value.open.return_value.__enter__.return_value.read.return_value=b'{"status":"0","infocode":"10001","info":"bad aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
            with self.assertRaises(AmapError) as error:
                request_json("/v5/place/text",{},"a"*32)
            self.assertNotIn("a"*32,str(error.exception))
            self.assertEqual(builder.call_args.args[0].proxies,{})
        with self.assertRaises(AmapError):
            request_json("https://example.com",{},"a"*32)

    def test_expired_cache_ignored(self):
        write_cache("x",{"rows":[]})
        self.assertIsNotNone(read_cache("x"))
        PlannerCache.objects.filter(cache_key="x").update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(read_cache("x"))

    def test_user_can_generate_new_city_without_import(self):
        pool=rows()
        upstream=[{"id":r["poi_id"],"name":r["name"],"cityname":r["city"],"adname":r["district"],"address":r["address"],
            "type":r["category"],"location":str(r["longitude"])+","+str(r["latitude"]),"business":{"rating":r["rating"],"opentime_week":r["hours_week"]}} for r in pool]
        def network(path,params,key):
            if path=="/v5/place/text":
                return {"status":"1","pois":upstream}
            return {"status":"1","route":{"transits":[{"duration":"600","distance":"1200","segments":[]}]}}
        data={k:str(v) if not isinstance(v,list) else v for k,v in preferences().items()}
        self.assertEqual(Attraction.objects.count(),0)
        with patch.dict("os.environ",{"TRAVEL_AMAP_KEY":"a"*32}),patch("travel.planner_api.request_json",side_effect=network):
            response=self.client.post("/plan/",data,follow=True)
        self.assertEqual(response.status_code,200)
        self.assertGreater(Attraction.objects.count(),0)
        result=self.client.session["travel_plan_result_v5"]
        self.assertGreater(result["stop_count"],0)
        self.assertGreater(result["verified_legs"],0)
        self.assertContains(response,"示例公园")
