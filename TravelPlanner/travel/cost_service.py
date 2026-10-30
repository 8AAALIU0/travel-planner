"""途牛只读查询。价格字段按官方文档；不提供下单入口。"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from .planner_api import cache_key, read_cache, write_cache

TOOLS = {'ticket': 'query_cheapest_tickets', 'hotel': 'tuniuHotelSearch',
         'train': 'searchLowestPriceTrain', 'flight': 'searchLowestPriceFlight'}
_lock = threading.Lock()
_last_call = 0.0
log = logging.getLogger(__name__)


class QuoteError(Exception):
    pass


def error_summary(obj):
    """保留可操作的业务错误码和提示，不显示整个请求或认证对象。"""
    data=obj.get('error') if isinstance(obj.get('error'),dict) else obj
    code=next((data[k] for k in ('errorCode','error_code','errCode','code','statusCode') if isinstance(data.get(k),(str,int))), '')
    message=next((data[k] for k in ('message','msg','error_msg','errMsg','errorMessage','description','reason') if isinstance(data.get(k),str) and data[k]), '')
    if not message and isinstance(obj.get('error'),str):message=obj['error']
    code=str(code)[:30]
    message=re.sub(r'sk-[A-Za-z0-9_-]+','[密钥已隐藏]',str(message))
    message=re.sub(r'(?i)(api[_-]?key|authorization|token|secret)([=: ]+)[^\s,;]+',r'\1=[隐藏]',message)
    message=re.sub(r'https?://\S+','[平台链接]',message)
    return ('错误码 '+code+'；' if code else '')+message[:320] if code or message else '平台没有提供具体错误说明'


def seat_count(value):
    if isinstance(value,bool):return None
    if isinstance(value,(int,float)) and value>=0:return int(value)
    match=re.fullmatch(r'(\d+)\+?',str(value).strip())
    return int(match.group(1)) if match else None


def normalize_datetime(value,day):
    text=str(value or '').strip()
    if re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?',text):
        try:
            parsed=datetime.strptime(text,'%H:%M:%S' if text.count(':')==2 else '%H:%M')
            return day+' '+parsed.strftime('%H:%M:%S')
        except ValueError:return text
    return text


def money(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 <= number <= 10000000:
            return number.quantize(Decimal('.01'))
    except (ValueError, InvalidOperation, TypeError):
        pass
    return None


def cli_command():
    node = shutil.which('node')
    if not node:
        raise QuoteError('没有找到 Node.js，请确认之前的安装还在，并重新打开终端启动网站。')
    candidates = []
    launcher = shutil.which('tuniu.cmd') or shutil.which('tuniu')
    if launcher:
        path = Path(launcher).resolve()
        candidates += [path, path.parent / 'node_modules/tuniu-cli/bin/tuniu.js']
    if os.environ.get('APPDATA'):
        candidates.append(Path(os.environ['APPDATA']) / 'npm/node_modules/tuniu-cli/bin/tuniu.js')
    for path in candidates:
        if path.is_file() and path.suffix == '.js':
            return [node, str(path)]
    raise QuoteError('没有找到途牛 CLI。请在 CMD 运行 where tuniu，核对是否仍然安装。')


def decode_result(text):
    """只接受整体 JSON；任何错误包装都不能穿透成成功数据。"""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        raise QuoteError('平台返回格式暂不能识别，未生成报价。') from None
    for _ in range(8):
        if not isinstance(obj, dict):
            break
        if obj.get('success') is False or obj.get('successCode') is False or obj.get('isError') is True or obj.get('error'):
            raise QuoteError('平台查询失败：'+error_summary(obj))
        if 'result' in obj:
            obj = obj['result']
            continue
        blocks = obj.get('content')
        if isinstance(blocks, list):
            candidates = []
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'text':
                    try:
                        candidates.append(json.loads(block.get('text', '')))
                    except (ValueError, TypeError):
                        pass
            if len(candidates) != 1:
                raise QuoteError('平台返回的正文不是单一 JSON，请保留结果供核对。')
            obj = candidates[0]
            continue
        return obj
    raise QuoteError('平台返回结构暂不能识别。')


def _connection_key(kind):
    fingerprint = hashlib.sha256(os.environ.get('TUNIU_API_KEY', '').strip().encode()).hexdigest()
    return 'travel-provider-connection:' + kind + ':' + fingerprint


def run_query(kind, args, wait_for_slot=False):
    started = time.monotonic()
    outcome = 'error'
    try:
        result = _run_query(kind, args, wait_for_slot=wait_for_slot)
        outcome = 'cache' if result[1] else 'provider'
        if not result[1]:
            cache.delete(_connection_key(kind))
        return result
    except QuoteError as exc:
        # Only connection failures/timeouts pause a service. Empty inventory,
        # malformed data, and errors for another service must not block it.
        if ('错误码 101' in str(exc) or '平台查询超时' in str(exc)):
            cache.set(_connection_key(kind), True, timeout=60)
        raise
    finally:
        log.warning('TRAVEL-TIMING provider=%s outcome=%s seconds=%.3f',
                    kind if kind in TOOLS else 'unknown', outcome, time.monotonic() - started)


def _run_query(kind, args, wait_for_slot=False):
    global _last_call
    if kind not in TOOLS:
        raise QuoteError('不支持该查询。')
    key = os.environ.get('TUNIU_API_KEY', '').strip()
    if not key:
        raise QuoteError('本次启动未填写途牛密钥。请停止网站，再用 manage.py runtravel 启动并填写途牛密钥。')
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    identifier = cache_key('tuniu-quotes-v8', [kind, args, fingerprint])
    cached = read_cache(identifier)
    if cached is not None:
        return cached, True
    if cache.get(_connection_key(kind)):
        raise QuoteError('该类途牛接口连接暂不可用，已暂停重复请求约1分钟；已有有效缓存仍可用，请稍后重试。本项未取得新报价。')
    with _lock:
        wait = 13 - (time.monotonic() - _last_call)
        if wait > 0:
            if not wait_for_slot:
                raise QuoteError('请间隔至少13秒再查另一项，避免触发平台频率限制。相同查询可使用缓存。')
            time.sleep(wait + .05)
        _last_call = time.monotonic()
    env = os.environ.copy()
    env.update(TUNIU_API_KEY=key, TUNIU_AUTH_TYPE='apiKey', NO_COLOR='1')
    try:
        reply = subprocess.run(cli_command() + ['call', kind, TOOLS[kind], '-a', json.dumps(args, ensure_ascii=False)],
                               shell=False, capture_output=True, encoding='utf-8', errors='replace', timeout=75,
                               env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise QuoteError('平台查询超时，未自动重试。请稍后再试。') from None
    except OSError:
        raise QuoteError('无法启动途牛查询程序，请检查 Node.js 和途牛 CLI。') from None
    text = reply.stdout.replace(key, '[密钥已隐藏]')
    if reply.returncode:
        combined = (text + reply.stderr).lower()
        if '401' in combined or 'authfailed' in combined:
            raise QuoteError('密钥验证失败，请使用当前有效的途牛 API Key 重新启动网站。')
        if '429' in combined or 'ratelimit' in combined:
            raise QuoteError('平台调用频率或额度限制，请稍后再试。')
        if reply.returncode == 101:
            raise QuoteError('错误码 101；无法连接途牛服务器，本项未取得报价。')
        try:
            decode_result(text)
        except QuoteError as exc:
            raise QuoteError('途牛命令未完成：'+str(exc)) from None
        raise QuoteError('途牛命令未成功完成。请核对查询条件、服务权限与调用额度。')
    data = decode_result(text)
    rows = normalize(kind, data, args)
    record = {'kind': kind, 'args': args, 'queried_at': timezone.localtime().isoformat(timespec='seconds'),
              'rows': rows, 'data': data, 'pages': data.get('totalPageNum', 1)}
    write_cache(identifier, record, hours=.25)
    return record, False


def normalize(kind, data, args):
    key = {'ticket':'tickets', 'hotel':'hotels', 'train':'data', 'flight':'data'}[kind]
    items = data.get(key)
    if not isinstance(items, list):
        raise QuoteError('平台成功响应中的列表字段与文档不一致；没有把缺失数据当成零费用。')
    rows = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        if kind == 'ticket':
            name = str(item.get('resName') or '未返回完整票名')
            first, last = str(item.get('startDate', ''))[:10], str(item.get('endDate', ''))[:10]
            try:
                valid = date.fromisoformat(first) <= date.fromisoformat(args['depart_date']) <= date.fromisoformat(last)
                valid = valid and date.fromisoformat(last) >= timezone.localdate()
            except (ValueError, KeyError):
                valid = False
            # 不能只凭景点名称和最低价选票；只开放普通成人观光票作为用户确认的预算参考。
            adult = '成人票' in name and not any(w in name for w in ['儿童','亲子','家庭','双人','多人','非中国大陆','港澳台'])
            adult = adult and not ('游船' in name and not any(w in name for w in ['观光','登塔','登高']))
            rows.append({'name': name, 'unit': str(money(item.get('startPrice'))) if money(item.get('startPrice')) is not None else '', 'basis':'起价，非指定日期实价',
                         'info':f"起价日期 {item.get('departsDate', '未返回')} · 可售范围 {first} ～ {last} · {item.get('personTypeName', '')}",
                         'usable': valid and adult, 'note':'请核对套餐、身份和日期；不能将游船价格视为登塔门票。', 'raw':item})
        elif kind == 'hotel':
            rows.append({'name':str(item.get('hotelName') or '未返回酒店名称'), 'unit':str(money(item.get('lowestPrice'))) if money(item.get('lowestPrice')) is not None else '',
                         'basis':'酒店搜索最低价，计价单位待核实',
                         'info':f"{item.get('roomName', '')} · {item.get('address', '')} · 评分 {item.get('commentScore', '未返回')}",
                         'usable':True, 'note':f"{item.get('meal', '')} · {item.get('refund', '')}。最低价不能直接认定为全程住宿总价。", 'raw':item})
        elif kind == 'train':
            prices, seats = item.get('price') or {}, item.get('seatAvailable') or {}
            if not isinstance(prices, dict) or not isinstance(seats, dict):
                continue
            seat_names = {'edz':'二等座','ydz':'一等座','yz':'硬座','yw':'硬卧','rw':'软卧','rz':'软座','swz':'商务座','tdz':'特等座','gjrw':'高级软卧','wz':'无座','dw':'动卧','ydw':'一等卧','edw':'二等卧'}
            for code, label in seat_names.items():
                price = money(prices.get(code+'Price'))
                if price is None:
                    continue
                num = seats.get(code+'Num')
                count=seat_count(num)
                available=count is not None and count>0
                departure = normalize_datetime(item.get('departureTime'),args['departureDate'])
                item=dict(item,departureTime=departure)
                arrival=normalize_datetime(item.get('arrivalTime'),args['departureDate'])
                if re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?',str(item.get('arrivalTime','')).strip()) and datetime.fromisoformat(arrival)<=datetime.fromisoformat(departure):
                    from datetime import timedelta
                    arrival=(date.fromisoformat(args['departureDate'])+timedelta(days=1)).isoformat()+' '+arrival.split(' ')[-1]
                item['arrivalTime']=arrival
                date_ok = departure.startswith(args['departureDate'])
                rows.append({'name':str(item.get('trainNum', '车次未返回'))+' · '+label, 'unit':str(price), 'basis':'每位成人单程票价',
                             'info':f"{item.get('departStationName', '')} → {item.get('destStationName', '')} · {departure} — {item.get('arrivalTime', '')} · {item.get('duration', '')}",
                             'usable':available and date_ok, 'note':f'余票原值：{num if num is not None else "未返回"}；余票及票价会变化，未含可选服务费。', 'raw':item})
        else:
            base, tax = money(item.get('basePrice')), money(item.get('totalTax'))
            total = base + tax if base is not None and tax is not None else None
            departure = normalize_datetime(item.get('departureTime'),args['departureDate'])
            item=dict(item,departureTime=departure,arrivalTime=normalize_datetime(item.get('arrivalTime'),args['departureDate']))
            count=seat_count(item.get('remainingSeats'))
            available=count is not None and count>0
            rows.append({'name':str(item.get('flightNumber') or '航班号未返回')+' · '+str(item.get('cabinClass') or ''),
                         'unit':str(total) if total is not None else '', 'basis':'每位成人单程：票面价＋税费',
                         'info':f"{item.get('departureAirport', '')} → {item.get('arrivalAirport', '')} · {departure} — {item.get('arrivalTime', '')}",
                         'usable':available and departure.startswith(args['departureDate']),
                         'note':f"票面价 {base if base is not None else '未知'}；税费 {tax if tax is not None else '未知'}；余票 {item.get('remainingSeats', '未知')}。不含未返回的附加费。", 'raw':item})
    for i, row in enumerate(rows):
        row['id'] = i
        row['usable'] = row['usable'] and money(row['unit']) is not None
    return rows


def load_saved_ticket(filename):
    folder = Path(settings.BASE_DIR) / 'ticket_results'
    if not re.fullmatch(r'tuniu_[0-9_]+\.json', filename):
        raise QuoteError('文件名不符合已保存结果格式。')
    path = folder / filename
    if not path.is_file() or path.stat().st_size > 2000000:
        raise QuoteError('结果文件不存在或超过2MB。')
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        args = record['query']
        date.fromisoformat(args['depart_date'])
        data = decode_result(json.dumps(record, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        raise QuoteError('历史文件格式不完整，无法作为报价参考。') from None
    return {'kind':'ticket', 'args':args, 'queried_at':record.get('queried_at', '未知'),
            'rows':normalize('ticket', data, args), 'data':data, 'pages':1}
