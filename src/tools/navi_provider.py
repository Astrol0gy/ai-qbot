"""
导航类 Tool Provider（高德地图 Web 服务 2.0）

提供两个工具：
- search_place：搜索 POI（关键词 / 周边），拿坐标、地址、评分、营业时间、电话
- plan_route：路径规划（驾车 / 步行 / 骑行 / 电动车 / 公交换乘），给距离与耗时

几条硬约定：
1. 坐标一律「经度,纬度」，与高德接口一致（注意不是常见的「纬度,经度」）。高德用 GCJ-02
   火星坐标系，与 GPS 的 WGS84 差几十到几百米，两种坐标别混着传。
2. 算耗时 / 过路费必须显式 `show_fields=cost`：高德**基础返回里没有耗时**，不加这个参数
   只能拿到距离（文档里最容易踩的坑）。
3. 地名可以直接给 plan_route，内部先搜一次 POI 换坐标；但只顺手解析一次，搜不到就明说，
   不硬猜坐标。
4. 失败快速降级：Key 没配 / 权限不足 / 超配额这几类重试没意义，直接说明原因并让模型别再试。
5. 公交的 city1/city2 **只认 citycode/adcode**，传中文城市名会被拒成 INVALID_PARAMS
   （2026-10-15 真跑实测：「成都」「成都市」失败，「028」「510100」成功）。所以 `city`/`city2`
   对外仍收中文名，内部由 _city_code 换码（数字原样用，中文名走一次 POI 搜索取 citycode）。
6. 公交返回的字段形状跟驾车不一样（同样是真跑核对）：transits[0] 上**没有** duration，
   耗时只在 cost.duration；票价是 cost.transit_fee 且形如 '3.0'；总步行在 walking_distance；
   步行段里**没有** destination_stop，只有 distance 与 cost.duration。
"""
import requests

from ..config import AMAP_KEY, AMAP_POI_LIMIT, AMAP_TIMEOUT_SEC
from . import ToolProvider

_SEARCH_TEXT_URL = "https://restapi.amap.com/v5/place/text"
_SEARCH_AROUND_URL = "https://restapi.amap.com/v5/place/around"

_ROUTE_URLS = {
    "driving": "https://restapi.amap.com/v5/direction/driving",
    "walking": "https://restapi.amap.com/v5/direction/walking",
    "bicycling": "https://restapi.amap.com/v5/direction/bicycling",
    "electrobike": "https://restapi.amap.com/v5/direction/electrobike",
    "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
}

_MODE_LABELS = {
    "driving": "驾车",
    "walking": "步行",
    "bicycling": "骑行",
    "electrobike": "电动车",
    "transit": "公交换乘",
}

# 周边搜索不给半径时的默认值（米）。高德默认 5000，但群里说「附近」一般没这么远，
# 3 公里更贴近「附近有啥吃的」这种问法
_DEFAULT_RADIUS = 3000

# 配置 / 配额类错误码：重试不会变好，直接劝停（依据高德错误码表）
_FATAL_CODES = {
    "10001": "Key 不正确或已过期（AMAP_KEY 填错了）",
    "10002": "这个 Key 没有该服务的权限（去高德控制台开通「搜索POI 2.0」/「路径规划 2.0」）",
    "10003": "今日调用量已达上限",
    "10005": "IP 白名单校验失败（Key 绑了 IP，但服务器出口 IP 不在白名单里）",
    "10009": "Key 与绑定的平台不符（Web 服务 Key 不能用于 JS/Android 等平台）",
    "10011": "该账号权限不足（未开通此服务）",
    "10026": "账号已被冻结",
    "10044": "账号维度的日调用量已超限",
}

# 这类是"一会儿再试就行"的临时问题，不能说成"重试没用"（否则会把限流误导成 Key 报废）
_TRANSIENT_CODES = {
    "10004": "调用过于频繁（QPS 超限），稍等一下再试",
    "10010": "单个 IP 的查询量超限，稍等一下再试",
    "10021": "实际访问量已达 QPS 上限，稍等一下再试",
}

_NO_KEY_MSG = (
    "导航功能没配置：缺少高德 Key（.env 里的 AMAP_KEY）。"
    "这是管理员该配的东西，重试没有意义 —— 直接告诉用户「暂时查不了地点和路线」，不要换个说法再试。"
)


def _s(value) -> str:
    """高德缺字段时给的是空数组 [] 而不是空字符串，统一成字符串"""
    if value is None or isinstance(value, (list, dict)):
        return ""
    return str(value).strip()


def _fmt_distance(meters: int) -> str:
    return f"{meters} 米" if meters < 1000 else f"{meters / 1000:.1f} 公里"


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = (seconds + 30) // 60  # 四舍五入到分钟：精确到秒对回答没有意义
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _fetch(url: str, params: dict) -> tuple[dict, str]:
    """发一次高德请求。返回 (响应 JSON, 错误文案)；成功时错误文案是空字符串"""
    try:
        resp = requests.get(url, params=params, timeout=AMAP_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout:
        return {}, f"高德接口超时（{AMAP_TIMEOUT_SEC:.0f} 秒没返回），这次没查到。"
    except requests.RequestException as e:
        return {}, f"高德接口请求失败：{e}"
    except ValueError:
        return {}, "高德接口返回的不是 JSON（可能被网关拦了）。"

    if str(data.get("status")) != "1":
        info = _s(data.get("info")) or "未知错误"
        code = _s(data.get("infocode"))
        base = f"高德接口返回失败：{info}（infocode={code}）"
        hint = _FATAL_CODES.get(code)
        if hint:
            return {}, f"{base}。原因：{hint}；这类问题重试没用，不要再调导航工具，直接告诉用户查不了。"
        # 限流这类是临时的：说成"重试没用"会把「等一会儿就好」误导成 Key 报废
        transient = _TRANSIENT_CODES.get(code)
        if transient:
            return {}, f"{base}。原因：{transient}；本次先别重复调用。"
        return {}, f"{base}。"
    return data, ""


def _is_lnglat(text: str) -> bool:
    """判断是不是「经度,纬度」形式的坐标（而不是地名）"""
    parts = text.split(",")
    if len(parts) != 2:
        return False
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return False
    return 0 <= lng <= 180 and 0 <= lat <= 90 and (lng != 0 or lat != 0)


def _geo(text: str, city: str = "") -> tuple[str, str, str]:
    """
    把起终点解析成坐标。返回 (坐标, 显示名, 错误文案)。

    已经是「经度,纬度」就直接用；否则搜一次 POI 取第一条 —— 只顺手解析一次，
    搜不到就明说没找到，不猜坐标（猜错的坐标会算出一条看着煞有介事的错路线）。
    """
    text = (text or "").strip().replace("，", ",")
    if not text:
        return "", "", "起点或终点是空的。"
    if _is_lnglat(text):
        return text, text, ""

    data, err = _fetch(
        _SEARCH_TEXT_URL,
        {"key": AMAP_KEY, "keywords": text, "page_size": 1,
         "city": city, "city_limit": "true" if city else "false"},
    )
    if err:
        return "", "", err
    pois = data.get("pois") or []
    if not pois:
        scope = f"（已限定在 {city}）" if city else ""
        return "", "", (
            f"没找到地点「{text}」{scope}：换个更具体的说法（如「成都东站」），"
            "或直接给坐标「经度,纬度」。"
        )
    poi = pois[0]
    location = _s(poi.get("location"))
    if not location:
        return "", "", f"「{text}」没有坐标信息，无法用来算路。"
    name = _s(poi.get("name")) or text
    area = f"{_s(poi.get('cityname'))}{_s(poi.get('adname'))}"
    return location, f"{name}（{area}）" if area else name, ""


def _fmt_money(value) -> str:
    """金额（票价 / 过路费）：高德给的是 '3.0'，群里说「3 元」才自然"""
    text = _s(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "" if text in ("", "-") else (text or "0")


def _norm_city(name: str) -> str:
    """城市名归一化：成都 / 成都市 都算「成都」"""
    return name.strip().rstrip("市")


def _city_code(value: str) -> tuple[str, str]:
    """
    把城市换成公交接口要的 citycode。返回 (code, 错误文案)。

    实测（2026-10-15）：``/v5/direction/transit/integrated`` 的 city1/city2 **只认
    citycode/adcode**，中文城市名一律 INVALID_PARAMS —— 「成都」「成都市」都失败，
    「028」「510100」都成功。但模型和用户嘴里只有中文城市名，所以这里自己换一次：
    纯数字直接用；中文名走一次 POI 搜索，从结果里取 citycode（搜「成都」首条是
    「成都市」，citycode=028）。这样不用额外开通「行政区划查询」服务。
    """
    text = (value or "").strip()
    if not text:
        return "", "城市是空的。"
    if text.isdigit():
        return text, ""

    data, err = _fetch(_SEARCH_TEXT_URL, {"key": AMAP_KEY, "keywords": text, "page_size": 5})
    if err:
        return "", err
    pois = data.get("pois") or []
    candidates = [(c, _s(p.get("cityname"))) for p in pois
                  if (c := _s(p.get("citycode")))]
    if not candidates:
        return "", (
            f"没能把城市「{text}」换成 citycode（公交接口只认 citycode/adcode，不认中文城市名）。"
            "可以直接传 citycode（成都 028、北京 010）或 adcode（成都 510100）。"
        )
    # 优先取城市名对得上的那条：搜「成都」会同时命中机场、车站等，虽然 citycode 相同，
    # 但「长安」这类歧义词就可能命中别的城市，先按名字比对一次更稳
    for code, cityname in candidates:
        if _norm_city(cityname) == _norm_city(text):
            return code, ""
    return candidates[0][0], ""


def _business_bits(business) -> list[str]:
    """评分 / 人均 / 营业时间 / 电话：只有 show_fields=business 才拿得到"""
    if not isinstance(business, dict):
        return []
    bits: list[str] = []
    rating, cost = _s(business.get("rating")), _s(business.get("cost"))
    if rating:
        bits.append(f"评分 {rating}" + (f"（人均 {cost} 元）" if cost else ""))
    elif cost:
        bits.append(f"人均 {cost} 元")
    opentime = _s(business.get("opentime_today")) or _s(business.get("opentime_week"))
    if opentime:
        bits.append(f"营业 {opentime}")
    tel = _s(business.get("tel"))
    if tel:
        bits.append(f"电话 {tel}")
    tag = _s(business.get("tag"))
    if tag:
        bits.append(f"标签 {tag}")
    return bits


def _format_pois(pois: list[dict], around: bool) -> str:
    blocks: list[str] = []
    for i, poi in enumerate(pois, 1):
        block = [f"{i}. {_s(poi.get('name')) or '(未命名地点)'}"]

        addr = _s(poi.get("address"))
        area = f"{_s(poi.get('pname'))}{_s(poi.get('cityname'))}{_s(poi.get('adname'))}"
        if addr:
            block.append(f"   地址：{addr}")
        elif area:
            block.append(f"   地址：{area}")

        location = _s(poi.get("location"))
        if location:
            block.append(f"   坐标：{location}（GCJ-02，可直接传给 plan_route）")

        tail: list[str] = []
        # 高德有时会把末级重复一遍（实测："购物服务;特色商业街;特色商业街"），顺手去重
        parts: list[str] = []
        for part in _s(poi.get("type")).split(";"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
        if parts:
            tail.append(f"类型：{' / '.join(parts)}")
        if around:
            distance = _s(poi.get("distance"))
            if distance.isdigit():
                tail.append(f"距离：{_fmt_distance(int(distance))}")
        if tail:
            block.append("   " + " ｜ ".join(tail))

        bits = _business_bits(poi.get("business"))
        if bits:
            block.append("   " + " ｜ ".join(bits))

        blocks.append("\n".join(block))

    title = "高德周边搜索结果（按距离从近到远）：" if around else "高德地点搜索结果："
    return title + "\n" + "\n".join(blocks)


def _stop_name(value) -> str:
    """站点字段有时是字符串、有时是 {name: ...}，统一取名字"""
    if isinstance(value, dict):
        return _s(value.get("name"))
    return _s(value)


def _cost_bits(path: dict, mode: str) -> list[str]:
    """耗时 / 过路费 / 红绿灯：全在 cost 里，必须 show_fields=cost"""
    cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
    bits: list[str] = []
    tolls = _fmt_money(cost.get("tolls"))
    if tolls and tolls != "0":
        bits.append(f"过路费 {tolls} 元")
    toll_distance = _s(cost.get("toll_distance"))
    if toll_distance.isdigit() and int(toll_distance) > 0:
        bits.append(f"收费路段 {_fmt_distance(int(toll_distance))}")
    lights = _s(cost.get("traffic_lights"))
    if mode == "driving" and lights.isdigit():
        bits.append(f"红绿灯 {lights} 个")
    return bits


def _format_path(path: dict, mode: str, origin_name: str, dest_name: str,
                 taxi_cost: str = "") -> str:
    cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
    lines = [f"{_MODE_LABELS[mode]}路线：{origin_name} → {dest_name}"]

    head: list[str] = []
    distance = _s(path.get("distance"))
    if distance.isdigit():
        head.append(f"距离 {_fmt_distance(int(distance))}")
    duration = _s(cost.get("duration"))
    if duration.isdigit():
        head.append(f"耗时约 {_fmt_duration(int(duration))}")
    # 驾车时高德在 route.taxi_cost 里给了打车估价，群里问「打车多少钱」正好用得上
    taxi = _fmt_money(taxi_cost)
    if mode == "driving" and taxi and taxi != "0":
        head.append(f"打车约 {taxi} 元")
    if head:
        lines.append(" ｜ ".join(head))

    bits = _cost_bits(path, mode)
    if bits:
        lines.append(" ｜ ".join(bits))

    if mode == "driving":
        restriction = _s(path.get("restriction"))
        # 实测：成都不限行时高德给的是 "0"。这种就别说话 —— 原来说「限行：已规避」
        # 是替高德下了没有依据的结论
        if restriction == "1":
            lines.append("注意：这段路有限行，出发前建议再确认（传了 plate 会按车牌规划）")
        elif restriction and restriction != "0":
            lines.append(f"限行提示：{restriction}")

    steps = path.get("steps") or []
    if steps:
        lines.append("")
        lines.append("路线：")
        shown = steps[:8]
        for i, step in enumerate(shown, 1):
            text = _s(step.get("instruction")) or _s(step.get("road_name")) or "继续前行"
            step_distance = _s(step.get("step_distance"))
            suffix = f"（{_fmt_distance(int(step_distance))}）" if step_distance.isdigit() else ""
            lines.append(f"   {i}. {text}{suffix}")
        if len(steps) > len(shown):
            lines.append(f"   …共 {len(steps)} 段，后面略")
    return "\n".join(lines)


def _format_transit(transit: dict, origin_name: str, dest_name: str) -> str:
    """
    公交方案。字段形状跟驾车不一样，这里是真跑核对过的（2026-10-15）：
    transits[0] 上**没有** duration，耗时只在 cost.duration 里（show_fields=cost 才有）；
    票价在 cost.transit_fee（高德给的是 '3.0' 这种带小数的字符串）；总步行在 walking_distance；
    步行段里**没有** destination_stop，只有 distance 和 cost.duration。
    """
    cost = transit.get("cost") if isinstance(transit.get("cost"), dict) else {}
    lines = [f"公交换乘：{origin_name} → {dest_name}"]

    head: list[str] = []
    distance = _s(transit.get("distance"))
    if distance.isdigit():
        head.append(f"距离 {_fmt_distance(int(distance))}")
    duration = _s(cost.get("duration")) or _s(transit.get("duration"))
    if duration.isdigit():
        head.append(f"耗时约 {_fmt_duration(int(duration))}")
    fare = _fmt_money(cost.get("transit_fee"))
    if fare and fare != "0":
        head.append(f"票价 {fare} 元")
    walking = _s(transit.get("walking_distance"))
    if walking.isdigit() and int(walking) > 0:
        head.append(f"全程步行 {_fmt_distance(int(walking))}")
    if head:
        lines.append(" ｜ ".join(head))

    segments = transit.get("segments") or []
    if segments:
        lines.append("")
        lines.append("换乘方案：")
    # 编号按"输出的行"走：一个 segment 里可能既有步行又有乘车（实测第一段就是），
    # 用 segment 下标会出现两个「1.」
    n = 0
    for seg in segments:
        walk = seg.get("walking") if isinstance(seg.get("walking"), dict) else None
        if walk:
            # 步行段没有 destination_stop（实测），只有距离和步行耗时
            bits: list[str] = []
            walk_distance = _s(walk.get("distance"))
            bits.append(f"步行 {_fmt_distance(int(walk_distance))}"
                        if walk_distance.isdigit() else "步行")
            walk_cost = walk.get("cost") if isinstance(walk.get("cost"), dict) else {}
            walk_duration = _s(walk_cost.get("duration"))
            if walk_duration.isdigit() and int(walk_duration) > 0:
                bits.append(f"约 {_fmt_duration(int(walk_duration))}")
            n += 1
            lines.append(f"   {n}. " + "，".join(bits))
        bus = seg.get("bus") if isinstance(seg.get("bus"), dict) else None
        if bus:
            for busline in bus.get("buslines") or []:
                name = _s(busline.get("name")) or "公交/地铁"
                dep = _stop_name(busline.get("departure_stop"))
                arr = _stop_name(busline.get("arrival_stop"))
                via = _s(busline.get("via_num"))
                ride = " → ".join(x for x in (dep, arr) if x)
                suffix = f"，{via} 站" if via.isdigit() else ""
                n += 1
                lines.append(f"   {n}. 乘坐 {name}" + (f"（{ride}{suffix}）" if ride else suffix))
        railway = seg.get("railway") if isinstance(seg.get("railway"), dict) else None
        if railway:
            n += 1
            lines.append(f"   {n}. 换乘火车/城际：{_s(railway.get('name')) or '见车次'}")
        taxi = seg.get("taxi") if isinstance(seg.get("taxi"), dict) else None
        if taxi:
            n += 1
            lines.append(f"   {n}. 打车：约 {_s(taxi.get('distance'))} 米，"
                         f"{_s(taxi.get('duration'))} 秒，预计 {_s(taxi.get('price'))} 元")
    return "\n".join(lines)


# ============================================
# Provider
# ============================================
class NaviProvider(ToolProvider):
    name = "navi"
    description = "地图导航工具：search_place（搜地点/商家，拿坐标与详情）、plan_route（算路线与耗时）"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "search_place",
                "description": (
                    "搜索地点 / 商家 / 兴趣点，拿到坐标、地址、评分、人均、营业时间、电话。两种用法：\n"
                    "- 按名字找：keywords='春熙路'（可配 city 限定城市）\n"
                    "- 找附近的：keywords='火锅' + near='经度,纬度'（near 有值就走周边搜索，按距离从近到远）\n"
                    "结果里的坐标是高德 GCJ-02 的「经度,纬度」，可以直接当作 plan_route 的起点/终点。\n"
                    "注意：keywords 只支持**一个**关键词且不超过 80 字符；用户问「XX 附近有什么」时，"
                    "得先拿到 XX 的坐标（用本工具按名字搜一次）再传 near。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "string",
                            "description": "地点/商家关键词，只支持一个词、最长 80 字符（如「春熙路」「小龙坎火锅」）。只按分类找时可不填，改用 types。",
                        },
                        "city": {
                            "type": "string",
                            "description": "限定城市的中文名（如「成都」）。填了就只在/优先该城市搜索，避免搜到外地的同名地点。",
                        },
                        "near": {
                            "type": "string",
                            "description": "「经度,纬度」：填了就是周边搜索（找这附近有什么），不填是按名字搜。坐标顺序是经度在前。",
                        },
                        "radius": {
                            "type": "integer",
                            "description": "周边搜索半径，单位米（1~50000），只在 near 有值时生效，默认 3000。",
                        },
                        "types": {
                            "type": "string",
                            "description": "POI 分类码（可选，如 050000 餐饮、060000 购物）。只有在明确要某一类时才用。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数 1~25，默认 5（群里够用，太多会挤占上下文）。",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "handler": self._search_place,
                # 每次都是网络往返，限 4 次：够「先搜坐标再确认」，不至于把一轮拖长
                "max_calls": 4,
            },
            {
                "name": "plan_route",
                "description": (
                    "算两地之间的路线：距离、预计耗时、过路费、红绿灯和主要路段。"
                    "支持驾车 / 步行 / 骑行 / 电动车 / 公交换乘。\n"
                    "起点和终点都可以是地名（如「成都东站」）或「经度,纬度」坐标；给地名时内部会先搜一次 POI "
                    "换坐标（所以不确定的名字建议先用 search_place 确认）。\n"
                    "公交换乘必须给 city（起终点城市，中文名如「成都」或 citycode 如 028 都行，"
                    "内部会换成 citycode）；跨城时 city=起点城市、city2=终点城市。\n"
                    "耗时与过路费取高德的 cost 字段（已自动请求），是规划值，实际以路况为准；"
                    "驾车时还会给打车估价（route.taxi_cost）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {
                            "type": "string",
                            "description": "起点：「经度,纬度」或地名（如「成都东站」）。",
                        },
                        "destination": {
                            "type": "string",
                            "description": "终点：「经度,纬度」或地名（如「双流机场」）。",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["driving", "walking", "bicycling", "electrobike", "transit"],
                            "description": "出行方式：driving 驾车（默认）/ walking 步行 / bicycling 骑行 / electrobike 电动车 / transit 公交换乘。",
                        },
                        "city": {
                            "type": "string",
                            "description": "起终点所在城市（如「成都」，也可直接给 citycode 如 028 / adcode 如 510100）。公交换乘必填；跨城时填起点城市。其他方式可不填，填了能提高地名解析的准确度。",
                        },
                        "city2": {
                            "type": "string",
                            "description": "终点城市，只在跨城公交换乘时需要（同城不用填）。写法同 city。",
                        },
                        "strategy": {
                            "type": "integer",
                            "description": "路线策略（可选，仅驾车/公交有效）：0 速度优先 / 1 费用优先 / 2 常规最快 / 32 高德推荐（默认）/ 33 躲避拥堵 / 34 高速优先 / 35 不走高速 / 36 少收费 / 37 大路优先。",
                        },
                        "waypoints": {
                            "type": "string",
                            "description": "途经点（可选）：多个用「;」分隔，最多 16 个，格式「经度,纬度」（不支持地名）。",
                        },
                        "plate": {
                            "type": "string",
                            "description": "车牌号（可选，如「川A12345」）：驾车时用来判断限行。",
                        },
                    },
                    "required": ["origin", "destination"],
                    "additionalProperties": False,
                },
                "handler": self._plan_route,
                "max_calls": 3,
            },
        ]

    @staticmethod
    def _search_place(keywords: str = "", city: str = "", near: str = "",
                      radius: int = 0, types: str = "", limit: int = 0) -> str:
        if not AMAP_KEY:
            return _NO_KEY_MSG

        keywords = (keywords or "").strip()
        types = (types or "").strip()
        city = (city or "").strip()
        near = (near or "").strip().replace("，", ",")

        if not keywords and not types:
            return "参数不足：keywords（地点/商家名）和 types（分类码）至少要给一个。"
        if len(keywords) > 80:
            return "关键词太长：高德只支持一个关键词且不超过 80 字符，请精简成具体的地点/店名。"

        count = max(1, min(25, limit or AMAP_POI_LIMIT))
        params = {
            "key": AMAP_KEY,
            "keywords": keywords,
            "types": types,
            "city": city,
            # 传了城市就严格限定（否则只是加权重，会搜出外地的同名地点）
            "city_limit": "true" if city else "false",
            "page_size": count,
            # 评分 / 人均 / 营业时间 / 电话都在 business 里，不传这些字段根本不存在
            "show_fields": "business",
        }
        if near:
            if not _is_lnglat(near):
                return f"near 要的是「经度,纬度」坐标，收到的是「{near}」。先用 keywords 搜到这个地方拿坐标。"
            params["location"] = near
            params["radius"] = max(1, min(50000, radius)) if radius else _DEFAULT_RADIUS
            params["sortrule"] = "distance"
            url = _SEARCH_AROUND_URL
        else:
            url = _SEARCH_TEXT_URL

        data, err = _fetch(url, params)
        if err:
            return err
        pois = data.get("pois") or []
        if not pois:
            what = keywords or f"分类 {types}"
            scope = f"（限定在 {city}）" if city else ""
            return f"没搜到「{what}」{scope}。换个更具体的名字试试，或去掉城市限定。"
        return _format_pois(pois[:count], around=bool(near))

    @staticmethod
    def _plan_route(origin: str, destination: str, mode: str = "driving", city: str = "",
                    city2: str = "", strategy: int = 0, waypoints: str = "",
                    plate: str = "") -> str:
        if not AMAP_KEY:
            return _NO_KEY_MSG

        mode = (mode or "driving").strip().lower()
        if mode not in _ROUTE_URLS:
            return f"不支持的方式「{mode}」：可选 driving / walking / bicycling / electrobike / transit。"

        city = (city or "").strip()
        city2 = (city2 or "").strip() or city

        origin_lnglat, origin_name, err = _geo(origin, city)
        if err:
            return f"起点有问题：{err}"
        dest_lnglat, dest_name, err = _geo(destination, city2)
        if err:
            return f"终点有问题：{err}"

        params = {
            "key": AMAP_KEY,
            "origin": origin_lnglat,
            "destination": dest_lnglat,
            # cost 才给 duration（耗时）和 tolls（过路费）：高德基础返回里没有耗时
            "show_fields": "cost",
        }
        if mode == "transit":
            if not city:
                return ("公交换乘需要 city：起终点所在城市（中文名如「成都」，"
                        "或 citycode 如 028）；跨城时 city=起点城市、city2=终点城市。")
            # 实测：公交的 city1/city2 只认 citycode/adcode，直接传「成都」会被拒成
            # INVALID_PARAMS，所以这里换一次码（数字原样用，中文名搜 POI 取 citycode）
            city_code, err = _city_code(city)
            if err:
                return f"公交起点城市有问题：{err}"
            city2_code = city_code if city2 == city else ""
            if not city2_code:
                city2_code, err = _city_code(city2)
                if err:
                    return f"公交终点城市有问题：{err}"
            params["city1"] = city_code
            params["city2"] = city2_code
            params["AlternativeRoute"] = 1  # 只取最推荐的一条方案，省上下文
            if strategy:
                params["strategy"] = strategy
        else:
            if strategy:
                params["strategy"] = strategy
            if waypoints:
                params["waypoints"] = waypoints.strip().replace("，", ",").replace(" ", "")
            if plate:
                params["plate"] = plate.strip()

        data, err = _fetch(_ROUTE_URLS[mode], params)
        if err:
            return err

        route = data.get("route") if isinstance(data.get("route"), dict) else {}
        if mode == "transit":
            transits = route.get("transits") or []
            if not transits:
                return (f"没有可用的公交换乘方案（{origin_name} → {dest_name}）："
                        "可能太近、太远，或者这段路没有公交可达。")
            return _format_transit(transits[0], origin_name, dest_name)

        paths = route.get("paths") or []
        if not paths:
            return (f"没有可用的路线（{origin_name} → {dest_name}）："
                    "可能起终点太近、被水域隔开，或这一段没有可达路网。")
        return _format_path(paths[0], mode, origin_name, dest_name, _s(route.get("taxi_cost")))
