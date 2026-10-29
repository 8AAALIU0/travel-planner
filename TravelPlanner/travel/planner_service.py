"""预算约束下的兴趣排序、邻近分组与交通核验；不是全局最优求解。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import math
import re
from urllib.parse import urlencode
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .amap_client import AmapError
from .collect_service import canonical_city, preview_rows, import_rows
from .models import AmapRecord
from .planner_forms import INTERESTS
from .planner_api import search_places, read_cache, write_cache, cache_key, route_between

KEYWORDS = {"nature": "公园", "culture": "历史文化", "museum": "博物馆", "city": "步行街"}
INTEREST_WORDS = {"nature": ("公园", "山", "湖", "湿地", "森林", "自然"), "culture": ("历史", "文化", "古", "寺", "故居", "遗址", "文物"), "museum": ("博物馆", "纪念馆", "美术馆"), "city": ("步行街", "商业街", "街区", "广场")}
ANNEX = ("停车场", "停车位", "售票", "检票", "入口", "出口", "出入口", "游客中心", "卫生间", "售货", "纪念品", "观光车", "游客服务", "售票处", "咖啡", "餐厅", "酒店")


def safe_candidate(row, city):
    if canonical_city(row.get("city", "")) != canonical_city(city):
        return False
    if row.get("longitude") is None or row.get("latitude") is None:
        return False
    if any(word in row["name"] for word in ANNEX):
        return False
    category = row.get("category", "")
    return any(word in category for word in ("风景名胜", "博物馆", "纪念馆", "美术馆", "特色商业街", "步行街", "游乐园", "动物园", "水族馆"))


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, [a["longitude"], a["latitude"], b["longitude"], b["latitude"]])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def fetch_pool(city, interests, key, keywords=None):
    aliases={"自然风光":"公园", "历史文化":"历史文化", "城市漫步":"步行街", "亲子":"游乐园", "拍照":"景区", "美食":"特色街区", "休闲":"公园"}
    queries = list(dict.fromkeys(aliases.get(w,w) for w in keywords)) if keywords else list(dict.fromkeys(KEYWORDS[i] for i in interests))
    if len(queries) == 1 and not keywords:
        queries.append("景区")
    queries = queries[:3]
    rows, warnings, cached_count = [], [], 0
    pending = []
    for query in queries:
        identifier = cache_key("places-v5", [canonical_city(city), query])
        cached = read_cache(identifier)
        if cached is not None:
            rows.extend(cached["rows"])
            cached_count += 1
        elif key:
            pending.append((query, identifier))
    if pending:
        # 线程只做 HTTP 和规范化，所有数据库操作留在主线程。
        def get(item):
            try:
                return item, preview_rows(search_places(key, city, item[0])), ""
            except AmapError as error:
                return item, [], str(error)
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(get, pending))
        for (query, identifier), found, error in responses:
            if error:
                warnings.append(query + "查询：" + error)
            else:
                stamp = timezone.now().isoformat()
                for row in found:
                    row["fetched_at"] = stamp
                write_cache(identifier, {"rows": found}, hours=6)
                rows.extend(found)
    # 无接口或部分查询失败时仍可使用该城市已关联高德的记录。
    for record in AmapRecord.objects.filter(attraction__city__in=[canonical_city(city), canonical_city(city)+"市"]).order_by("-fetched_at")[:100]:
        row = {f.name: getattr(record, f.name) for f in AmapRecord._meta.fields if f.name not in ("id", "attraction")}
        row["rating"] = str(record.rating) if record.rating is not None else None
        row["average_cost"] = str(record.average_cost) if record.average_cost is not None else None
        row["fetched_at"] = record.fetched_at.isoformat()
        rows.append(row)
    if not key:
        warnings.append("本次启动没有高德 Key，只使用已有资料及有效缓存；新城市请用 runtravel 启动后再生成。")
    unique = {}
    for row in rows:
        if safe_candidate(row, city):
            old = unique.get(row["poi_id"])
            if old is None or parse_datetime(row["fetched_at"]) > parse_datetime(old["fetched_at"]):
                unique[row["poi_id"]] = dict(row)
    candidates = list(unique.values())
    # 自动保存通过基础筛选的接口资料；不要求用户打开采集页。
    for offset in range(0, len(candidates), 25):
        batch = candidates[offset:offset+25]
        stamp = timezone.now().isoformat()
        # 每行保留原始获取时间，避免把缓存时间伪装成新请求时间。
        grouped = {}
        for row in batch:
            grouped.setdefault(row.get("fetched_at", stamp), []).append(row)
        for fetched, subset in grouped.items():
            clean = [{k: v for k, v in r.items() if k != "fetched_at"} for r in subset]
            report = import_rows({"rows": clean, "fetched_at": fetched}, [r["poi_id"] for r in clean])
            if report["skipped"]:
                warnings.append(f"有 {report['skipped']} 条资料因同名匹配或字段冲突未自动入库，推荐仍以接口候选为准。")
    return candidates, warnings, cached_count


def enrich(rows, preferences):
    records = {r.poi_id: r for r in AmapRecord.objects.select_related("attraction").filter(poi_id__in=[r["poi_id"] for r in rows])}
    today = timezone.localdate()
    enriched = []
    for row in rows:
        row = dict(row)
        row["detail_pk"] = None
        record = records.get(row["poi_id"])
        spot = record.attraction if record else None
        if spot:
            row["detail_pk"] = spot.pk
        text = row["name"] + row.get("category", "")
        matches = [i for i in preferences["interests"] if any(w in text for w in INTEREST_WORDS[i])]
        row["score"] = len(matches)*3 + (float(row["rating"]) if row.get("rating") is not None else 0)
        row["reason"] = "符合你的" + "、".join(dict(INTERESTS)[i] for i in matches) + "偏好" if matches else "作为目的地内的补充候选"
        row["visit_minutes"] = spot.visit_minutes if spot and spot.visit_minutes else (120 if "博物馆" in text or "公园" in text else 90)
        row["duration_estimated"] = not bool(spot and spot.visit_minutes)
        row["price_reference"] = bool(spot and not spot.is_demo and spot.ticket_price is not None and spot.ticket_source_url and spot.ticket_type and spot.ticket_checked_on and 0 <= (today-spot.ticket_checked_on).days <= 90)
        row["ticket_per_person"] = str(spot.ticket_price if row["price_reference"] else preferences["unknown_ticket"])
        row["ticket_source_url"] = spot.ticket_source_url if row["price_reference"] else ""
        row["ticket_type"] = spot.ticket_type if row["price_reference"] else "未知票价，按你设置的金额预留"
        if preferences.get('ticket_policy'):
            from .ticket_budgeting import apply
            row=apply(row,preferences,preferences['start_date'])
        if preferences.get("keywords"):
            terms={"自然风光":("公园","湖","湿地","森林","山岳"),"亲子":("亲子","儿童","游乐园","乐园","动物园","水族馆","科技馆"),
                   "历史文化":("历史","文化","古迹","遗址","文物","寺庙","故居"),"博物馆":("博物馆","纪念馆","科技馆","美术馆"),
                   "城市漫步":("步行街","街区","商业街","广场")}
            found=[w for w in preferences["keywords"] if any(t in text for t in terms.get(w,(w,)))]
            row["keyword_matches"]=found
            row["score"]=(float(row["rating"]) if row.get("rating") is not None else 0)+4*len(found)
            row["reason"] = ("符合“"+"、".join(found)+"”偏好；" if found else "兴趣关键词检索的补充候选；") + "类别：" + row.get("category", "待补充")
        row["score"] += .3 if row["price_reference"] else 0
        enriched.append(row)
    enriched.sort(key=lambda r: (-r["score"], r["name"]))
    # 近距离同名/包含关系地点保留一个，避免同一主景点的别名重复推荐。
    deduped = []
    for row in enriched:
        name = re.sub(r"[\s()（）·-]", "", row["name"])
        duplicate = False
        for old in deduped:
            old_name = re.sub(r"[\s()（）·-]", "", old["name"])
            if (name in old_name or old_name in name) and distance(row, old) < .5:
                duplicate = True
                break
        if not duplicate:
            deduped.append(row)
    return deduped


def time_text(minutes):
    return f"{minutes//60:02d}:{minutes%60:02d}"


def monday_closed(row, date):
    hours = re.sub(r"\s", "", row.get("hours_week", ""))
    return date.weekday() == 0 and bool(re.search(r"(?:周一|星期一)(?:全天)?(?:闭馆|闭园|关闭|不开放)", hours))


def opening_window(row):
    # 只解析完全明确的单一时段；旺淡季、节假日等复杂文本保持待核对。
    text = row.get("hours_week", "").strip()
    match = re.fullmatch(r"(?:(?:每天|每日|周一至周日)\s*)?(\d{1,2}):(\d{2})\s*[-—~至]\s*(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    h1,m1,h2,m2 = map(int, match.groups())
    if 0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59:
        a,b = h1*60+m1,h2*60+m2
        return (a,b) if b>a else None
    return None


def estimate_leg(start, end, mode):
    km = distance(start,end)
    speed = 4 if mode == "walking" else 18 if mode == "transit" else 25
    minutes = math.ceil(km*1.4/speed*60) + (20 if mode == "transit" else 10 if mode == "driving" else 0)
    return {"minutes": max(1,minutes), "km": round(km,2), "verified": False,
        "summary": "按直线距离粗估，尚未核验道路可达性", "fetched_at": ""}


def route_link(start, end, mode):
    return "https://uri.amap.com/navigation?" + urlencode({"from": f"{start['longitude']:.6f},{start['latitude']:.6f},{start['name']}", "to": f"{end['longitude']:.6f},{end['latitude']:.6f},{end['name']}", "mode": {"transit":"bus","walking":"walk","driving":"car"}[mode], "src":"travelplanner", "callnative":0})


def day_window(p, index):
    start=9*60
    end=17*60 if p["pace"]=="relaxed" else 18*60
    arrival_day=p.get("arrival_day_offset",0)
    if index<arrival_day:
        start=24*60
    elif index==arrival_day and p.get("arrival_minutes") is not None:
        start=max(start,p["arrival_minutes"])
    if index==p["days"]-1 and p.get("return_cutoff") is not None:
        end=min(end,p["return_cutoff"])
    return start,end


def build_plan(p, key):
    people, count = p["people"], p["days"]
    nights = p.get("nights") if p.get("nights") is not None else count-1
    from .stay_policy import lodging_cost
    hotel=lodging_cost(p,nights)
    food = p["food"] * people * count
    transport = p["transport"] * people * count
    reserve = (p["budget"]*Decimal("0.10")).quantize(Decimal("0.01"))
    base = hotel + food + transport + p["intercity"]
    result = {"city": p["city"], "start_date": p["start_date"].isoformat(), "people": people,
        "requested_days": count, "budget": str(p["budget"]), "days": [], "warnings": [], "alternatives": [],
        "created_at": timezone.now().astimezone().isoformat(timespec="seconds"), "candidate_count":0,
        "costs": {"hotel":str(hotel),"food":str(food),"transport":str(transport),"intercity":str(p["intercity"]),"reserve":str(reserve),"tickets":"0.00"},
        "status":"draft", "mode": p["mode"], "mode_label": {"transit":"公共交通","walking":"步行","driving":"驾车"}[p["mode"]]}
    if base+reserve > p["budget"]:
        result.update(status="over_budget", total=str(base+reserve), remaining=str(p["budget"]-base-reserve))
        result["warnings"].append("住宿、餐饮、交通预留和10%预备金已超过总预算；请降低这些标准、缩短天数或增加预算。此次没有请求景点接口。")
        return result
    if p.get("keywords"):
        rows, warnings, cache_hits = fetch_pool(p["city"],p["interests"],key,keywords=p["keywords"])
    else:
        rows, warnings, cache_hits = fetch_pool(p["city"], p["interests"], key)
    result["warnings"].extend(warnings)
    if p.get("ticket_refs"):
        result["warnings"].append("已选途牛票种仅按同名景点及当天日期匹配；未排入或日期变化的票种不计入。匹配票种仍为起价参考，不能保证当天实价或库存。")
    candidates = enrich(rows, p)
    result["candidate_count"] = len(candidates)
    result["cache_hits"] = cache_hits
    from .itinerary_optimizer import optimize
    optimized=optimize(candidates,p,p["budget"]-base-reserve,key,route_between)
    result["days"]=optimized["days"]
    result["optimizer"]=optimized["optimizer"]
    verified,unverified=optimized["verified_legs"],optimized["unverified_legs"]
    if optimized["slow_repaired"]:
        result["warnings"].append("核验发现部分交通耗时偏长，已重新比较其他景点组合，必要时才缩减景点数量。")
    stops=[s for d in result["days"] for s in d["stops"]]
    if p.get('ticket_policy'):
        kinds=[s.get('ticket_estimate',{}).get('kind','estimate') for s in stops]
        result['ticket_summary']={'official':sum(k in ('official','record') for k in kinds),'platform':kinds.count('platform'),'estimated':kinds.count('estimate')}
        if result['ticket_summary']['estimated']:result['warnings'].append(str(result['ticket_summary']['estimated'])+'个景点尚未取得有效票价资料，已按明确标注的规则估算；这部分不是查到的实际售价。')
    ticket_total=sum((Decimal(s["group_ticket"]) for s in stops),Decimal(0))
    result["costs"]["tickets"]=str(ticket_total)
    total=base+reserve+ticket_total
    actual_ids={s["poi_id"] for s in stops}
    result["alternatives"]=[{k:r.get(k) for k in ("poi_id","name","reason","rating","detail_pk")} for r in candidates if r["poi_id"] not in actual_ids][:6]
    result.update(total=str(total),remaining=str(p["budget"]-total),stop_count=len(stops),verified_legs=verified,unverified_legs=unverified,
        unknown_tickets=sum(not s["price_reference"] for s in stops),status="draft" if stops else "insufficient")
    if any(not d["stops"] for d in result["days"]):
        result["warnings"].append("部分日期没有排入景点：可能是候选不足、预算不足、闭馆提示或交通距离过大；没有强行填满。")
    if unverified:
        result["warnings"].append("部分景点间交通未核验，仅作粗估；请点击高德路线链接核对后再出发。")
    if not stops:
        result["warnings"].append("未找到满足当前条件的安排。请调整兴趣、预算或交通方式后重新生成。")
    result["warnings"]=list(dict.fromkeys(result["warnings"]))
    return result
