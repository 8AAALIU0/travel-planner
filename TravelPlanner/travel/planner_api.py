"""固定高德域名的 HTTPS 请求；显式直连，保留证书验证。"""
import hashlib
import json
import math
import socket
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener
from django.utils import timezone
from .models import PlannerCache
from .amap_client import AmapError, text_value


def cache_key(prefix, data):
    return hashlib.sha256((prefix + json.dumps(data, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()


def read_cache(identifier):
    item = PlannerCache.objects.filter(cache_key=identifier, expires_at__gt=timezone.now()).first()
    return item.payload if item else None


def read_cache_many(identifiers):
    """Fetch the whole candidate route matrix in one database round trip."""
    identifiers = list(set(identifiers))
    if not identifiers:
        return {}
    return dict(PlannerCache.objects.filter(
        cache_key__in=identifiers, expires_at__gt=timezone.now()
    ).values_list('cache_key', 'payload'))


def write_cache(identifier, payload, hours=6):
    PlannerCache.objects.update_or_create(cache_key=identifier, defaults={"payload": payload, "expires_at": timezone.now() + timedelta(hours=hours)})


def request_json(path, params, key):
    if path not in ("/v5/place/text", "/v5/place/around", "/v3/direction/transit/integrated", "/v3/direction/walking", "/v3/direction/driving"):
        raise AmapError("不支持的接口。")
    params = {**params, "key": key, "output": "json"}
    try:
        # 仅本次固定高德请求不使用自动代理，不影响浏览器或其他服务。
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request("https://restapi.amap.com" + path + "?" + urlencode(params)), timeout=12) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise AmapError("高德返回内容过大。")
        result = json.loads(raw.decode("utf-8-sig"))
    except HTTPError as error:
        raise AmapError(f"高德接口 HTTP {error.code}，本次未取得数据。") from None
    except (URLError, OSError, TimeoutError, socket.timeout):
        raise AmapError("连接高德失败或超时，请检查网络后再试。") from None
    except (ValueError, UnicodeError):
        raise AmapError("高德返回内容无法解析。") from None
    if not isinstance(result, dict):
        raise AmapError("高德响应格式异常。")
    if str(result.get("status")) != "1":
        code = text_value(result.get("infocode"), 80).replace(key, "[隐藏]")
        info = text_value(result.get("info"), 200).replace(key, "[隐藏]")
        raise AmapError(f"高德接口 {code}：{info}。该服务权限或配额需单独核对。")
    return result


def search_places(key, city, keyword):
    result = request_json("/v5/place/text", dict(keywords=keyword, region=city, city_limit="true", show_fields="business,photos", page_size=25, page_num=1), key)
    if not isinstance(result.get("pois"), list):
        raise AmapError("景点列表格式异常。")
    return result["pois"][:25]


def route_between(key, start, end, mode, city, use_cache=True):
    origin = f"{start['longitude']:.6f},{start['latitude']:.6f}"
    destination = f"{end['longitude']:.6f},{end['latitude']:.6f}"
    identifier = cache_key("route-v8", [origin, destination, mode, city])
    cached = read_cache(identifier) if use_cache else None
    if cached:
        return cached
    if not key:
        raise AmapError("没有设置高德 Key，交通耗时尚未查询。")
    params = dict(origin=origin, destination=destination)
    if mode == "transit":
        path = "/v3/direction/transit/integrated"
        params.update(city=city, extensions="all", strategy=0)
    elif mode == "walking":
        path = "/v3/direction/walking"
        params.update(origin_id=start["poi_id"], destination_id=end["poi_id"])
    elif mode == "driving":
        path = "/v3/direction/driving"
        params.update(originid=start["poi_id"], destinationid=end["poi_id"], extensions="all", strategy=0)
    else:
        raise AmapError("不支持的交通方式。")
    data = request_json(path, params, key)
    route = data.get("route")
    routes = route.get("transits" if mode == "transit" else "paths") if isinstance(route, dict) else None
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise AmapError("这两个地点之间没有返回可用交通方案。")
    selected = routes[0]
    try:
        seconds = float(selected["duration"])
        metres = float(selected["distance"])
        if not math.isfinite(seconds) or not math.isfinite(metres) or seconds < 0 or metres < 0 or seconds > 86400:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise AmapError("交通方案缺少有效耗时或距离。") from None
    lines = []
    for segment in selected.get("segments", []) if isinstance(selected.get("segments"), list) else []:
        bus = segment.get("bus", {}) if isinstance(segment, dict) else {}
        if isinstance(bus, dict) and isinstance(bus.get("buslines"), list):
            for line in bus["buslines"][:1]:
                if isinstance(line, dict) and text_value(line.get("name")):
                    lines.append(text_value(line.get("name"), 120))
    result = {"minutes": max(1, math.ceil(seconds / 60)), "km": round(metres / 1000, 2),
        "summary": " → ".join(lines[:4]), "verified": True, "fetched_at": timezone.now().isoformat()}
    from .route_details import parse_details
    result.update(parse_details(selected,mode))
    if use_cache:
        write_cache(identifier, result, hours=1)
    return result


def nearby_metro(key,point):
    params={'location':f"{point['longitude']:.6f},{point['latitude']:.6f}",'keywords':'地铁站','radius':1500,'sortrule':'distance','page_size':5,'page_num':1}
    return request_json('/v5/place/around',params,key).get('pois',[])
