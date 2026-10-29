"""供网页使用的分页查询。固定高德主机，不接收任意请求地址。"""
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .amap_client import AmapError, text_value


def search_page(key, city, keyword, page):
    if not 1 <= page <= 8:
        raise AmapError("页码应为 1—8。")
    parameters = dict(key=key, keywords=keyword, region=city, city_limit="true", show_fields="business,photos", page_size=25, page_num=page, output="json")
    try:
        with urlopen(Request("https://restapi.amap.com/v5/place/text?" + urlencode(parameters)), timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise AmapError("返回内容过大，未处理本次数据。")
        data = json.loads(raw.decode("utf-8-sig"))
    except HTTPError as error:
        raise AmapError(f"高德请求失败，HTTP {error.code}。请检查服务状态。") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise AmapError("网络连接失败或超时，请稍后再试。") from None
    except (UnicodeError, ValueError):
        raise AmapError("高德响应无法解析，请稍后再试。") from None
    if not isinstance(data, dict):
        raise AmapError("高德响应格式异常。")
    if str(data.get("status")) != "1":
        code = text_value(data.get("infocode"), 100).replace(key, "[隐藏]")
        info = text_value(data.get("info"), 200).replace(key, "[隐藏]")
        raise AmapError(f"高德返回 {code}：{info}。请核对 Web 服务权限及配额，不必反复点击。")
    pois = data.get("pois")
    if not isinstance(pois, list):
        raise AmapError("高德 pois 字段格式异常。")
    return [poi for poi in pois[:25] if isinstance(poi, dict)]
