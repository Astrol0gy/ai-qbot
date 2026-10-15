#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
navi provider 离线验证：POI 搜索与路径规划的解析、参数、降级行为。

全部离线（不连高德）：把 navi_provider 里的 requests 换成打桩实现 —— 既不用花 Key 的
配额，也能断言"到底发了哪些参数"，尤其是 show_fields=cost（漏了就没有耗时）和
city_limit（漏了会搜出外地的同名店）。

    python tests/navi_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests as real_requests                              # noqa: E402
from src.tools import execute, get_definitions, get_max_calls  # noqa: E402
from src.tools import navi_provider as nv                      # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 打桩 requests：CALLS 记录请求，PLAN 依序返回响应
# ============================================================
CALLS: list[tuple[str, dict]] = []
PLAN: list = []


class _Resp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise real_requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeRequests:
    """只替换 get；异常类沿用真实的，保证 except 分支抓到的是同一种异常"""

    RequestException = real_requests.RequestException
    HTTPError = real_requests.HTTPError
    Timeout = real_requests.Timeout
    ConnectionError = real_requests.ConnectionError

    @staticmethod
    def get(url, params=None, timeout=None):
        CALLS.append((url, dict(params or {})))
        if not PLAN:
            raise AssertionError(f"没有预备响应了，却还在请求：{url}")
        item = PLAN.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):      # (payload, status_code)：模拟 HTTP 错误
            payload, status = item
            return _Resp(payload, status)
        return _Resp(item)


nv.requests = _FakeRequests
nv.AMAP_KEY = "test-key"      # 打桩不需要真 Key


def reset(*payloads) -> None:
    CALLS.clear()
    PLAN.clear()
    PLAN.extend(payloads)


def calls_to(suffix: str) -> list[dict]:
    return [p for url, p in CALLS if url.endswith(suffix)]


def called(suffix: str) -> int:
    return sum(1 for url, _ in CALLS if url.endswith(suffix))


# ============================================================
# 响应样例（字段形状取自高德 v5 文档）
# ============================================================
POIS = {
    "status": "1", "info": "OK", "infocode": "10000", "count": "2",
    "pois": [
        # type 末级重复是高德常见的写法，输出时要合并
        {"name": "春熙路", "type": "购物服务;特色商业街;特色商业街",
         "location": "104.081234,30.657123", "address": "锦江区春熙路",
         "pname": "四川省", "cityname": "成都市", "adname": "锦江区",
         # citycode 是公交接口唯一认的城市写法，_city_code 就靠它把「成都」换成 028
         "citycode": "028", "adcode": "510104",
         "business": {"rating": "4.5", "cost": "0", "tel": "028-88888888",
                      "opentime_today": "09:00-22:00", "tag": "步行街"}},
        {"name": "另一个春熙路店", "type": "餐饮服务;火锅",
         "location": "104.081300,30.657200",
         "address": [],       # 高德没数据时给的是空数组，不是空字符串
         "pname": "四川省", "cityname": "成都市", "adname": "锦江区",
         "citycode": "028", "adcode": "510104"},
    ],
}

# 搜「重庆」的返回：用来验证跨城时 city2 走自己的解析（citycode=023 不一样）
POIS_CQ = {
    "status": "1", "info": "OK", "infocode": "10000", "count": "1",
    "pois": [{"name": "重庆市", "location": "106.5516,29.5630", "cityname": "重庆市",
              "citycode": "023", "adcode": "500000"}],
}

AROUND = {
    "status": "1", "info": "OK", "infocode": "10000", "count": "2",
    "pois": [
        {"name": "近的火锅店", "location": "104.081,30.657", "address": "某街 1 号",
         "type": "餐饮服务;火锅", "distance": "350"},
        {"name": "远的火锅店", "location": "104.09,30.66", "address": "某街 9 号",
         "type": "餐饮服务;火锅", "distance": "2100"},
    ],
}

DRIVING = {
    "status": "1", "info": "OK", "infocode": "10000", "count": "1",
    "route": {"origin": "104.081234,30.657123", "destination": "103.95,30.58",
              # taxi_cost 是 route 上的打车估价（真跑实测 21）
              "taxi_cost": "21",
              "paths": [{"distance": "12345", "restriction": "0",
                         "cost": {"duration": "1500", "tolls": "15",
                                  "toll_distance": "3000", "traffic_lights": "23"},
                         "steps": [{"instruction": f"第{i}段：沿人民南路向西",
                                    "step_distance": "200"} for i in range(1, 11)]}]},
}

# 有限行的那种：restriction="1"，措辞要谨慎（不许说「已规避」这种没依据的结论）
DRIVING_LIMITED = {
    "status": "1", "info": "OK", "infocode": "10000",
    "route": {"paths": [{"distance": "12345", "restriction": "1",
                         "cost": {"duration": "1500"}, "steps": []}]},
}

TRANSIT = {
    # 形状按 2026-10-15 真跑实测：transits[0] 上没有 duration，耗时只在 cost.duration；
    # 票价是 cost.transit_fee（'4.0' 这种），步行段里没有 destination_stop
    "status": "1", "info": "OK", "infocode": "10000",
    "route": {"transits": [{
        "distance": "8500", "walking_distance": "900", "nightflag": "0",
        "cost": {"duration": "2400", "transit_fee": "4.0"},
        "segments": [
            {"walking": {"distance": "300", "cost": {"duration": "240"}},
             "bus": {"buslines": [{"name": "地铁2号线(犀浦--龙泉驿)",
                                   "departure_stop": {"name": "春熙路"},
                                   "arrival_stop": {"name": "天府广场"},
                                   "via_num": "2"}]}},
            {"walking": {"distance": "500"}},
        ]}]},
}

EMPTY = {"status": "1", "info": "OK", "infocode": "10000", "count": "0", "pois": []}


# ============================================================
# 1. 工具注册与 schema
# ============================================================
section("工具注册（走真实 registry）")
defs = {d["function"]["name"]: d["function"] for d in get_definitions()}
check("search_place / plan_route 已注册",
      "search_place" in defs and "plan_route" in defs, f"共 {len(defs)} 个工具")
check("search_place 调用上限 4", get_max_calls("search_place") == 4)
check("plan_route 调用上限 3", get_max_calls("plan_route") == 3)
check("plan_route 必填 origin+destination",
      defs["plan_route"]["parameters"].get("required") == ["origin", "destination"])
check("search_place 无必填（keywords/types 二选一由 handler 兜）",
      defs["search_place"]["parameters"].get("required") == [])
check("mode 是枚举", "enum" in defs["plan_route"]["parameters"]["properties"]["mode"])
check("additionalProperties 关闭", defs["search_place"]["parameters"].get("additionalProperties") is False)


# ============================================================
# 2. 没配 Key
# ============================================================
section("缺少 AMAP_KEY")
real_key, nv.AMAP_KEY = nv.AMAP_KEY, ""
reset(EMPTY)
out = execute("search_place", '{"keywords": "春熙路"}')
check("明确说没配置且不发请求", "AMAP_KEY" in out and "没配置" in out and CALLS == [], out[:36])
out = execute("plan_route", '{"origin": "成都东站", "destination": "双流机场"}')
check("plan_route 同样明确回报", "AMAP_KEY" in out and CALLS == [])
nv.AMAP_KEY = real_key


# ============================================================
# 3. 关键词搜索
# ============================================================
section("search_place：按名字搜")
reset(POIS)
out = execute("search_place", '{"keywords": "春熙路", "city": "成都", "limit": 2}')
p = calls_to("/place/text")[0] if called("/place/text") == 1 else {}
check("走关键词搜索接口", called("/place/text") == 1 and called("/place/around") == 0)
check("传了 keywords / city", p.get("keywords") == "春熙路" and p.get("city") == "成都")
check("city_limit=true（严格限定城市）", p.get("city_limit") == "true")
check("show_fields=business（否则没有评分营业时间）", p.get("show_fields") == "business")
check("limit 落到 page_size", p.get("page_size") == 2)
check("名称/地址/坐标都在",
      "春熙路" in out and "锦江区春熙路" in out and "104.081234,30.657123" in out)
check("坐标标明是 GCJ-02 且可传给 plan_route", "GCJ-02" in out and "plan_route" in out)
check("评分/营业时间/电话都在",
      "评分 4.5" in out and "营业 09:00-22:00" in out and "028-88888888" in out)
check("空数组地址不显示成 []", "地址：[]" not in out and "另一个春熙路店" in out)
check("类型末级重复时合并", "购物服务 / 特色商业街" in out
      and "特色商业街 / 特色商业街" not in out, out.splitlines()[3] if out else "")

reset(POIS)
execute("search_place", '{"keywords": "春熙路"}')
check("无 city 时 city_limit=false", calls_to("/place/text")[0].get("city_limit") == "false")

reset(POIS)
execute("search_place", '{"keywords": "春熙路", "limit": 100}')
check("limit 越界夹到 25", calls_to("/place/text")[0].get("page_size") == 25)

reset(EMPTY)
out = execute("search_place", '{"keywords": "不存在的地方xyz", "city": "成都"}')
check("搜不到时说明没搜到（带上城市限定）", "没搜到" in out and "成都" in out, out[:44])


# ============================================================
# 4. 周边搜索与参数校验
# ============================================================
section("search_place：周边搜 / 参数校验")
reset(AROUND)
out = execute("search_place", '{"keywords": "火锅", "near": "104.081,30.657", "radius": 3000}')
p = calls_to("/place/around")[0] if called("/place/around") == 1 else {}
check("走周边搜索接口", called("/place/around") == 1 and called("/place/text") == 0)
check("传了 location / radius", p.get("location") == "104.081,30.657" and p.get("radius") == 3000)
check("sortrule=distance", p.get("sortrule") == "distance")
check("输出带距离且说明按距离排",
      "距离：350 米" in out and "距离：2.1 公里" in out and "按距离从近到远" in out)

reset(AROUND)
execute("search_place", '{"keywords": "火锅", "near": "104.081,30.657"}')
check("不给半径用默认 3000", calls_to("/place/around")[0].get("radius") == nv._DEFAULT_RADIUS)

reset(AROUND)
out = execute("search_place", '{"keywords": "火锅", "near": "春熙路"}')
check("near 传地名：要求先拿坐标且不发请求", "经度,纬度" in out and CALLS == [])
reset(AROUND)
out = execute("search_place", '{"keywords": "火锅", "near": "0,0"}')
check("0,0 不算有效坐标", CALLS == [] and "经度,纬度" in out)

reset(POIS)
out = execute("search_place", '{"keywords": "' + "长" * 81 + '"}')
check("关键词超 80 字符：回报且不发请求", "80" in out and CALLS == [])

reset(POIS)
out = execute("search_place", '{}')
check("keywords 与 types 都空：说明参数不足", "至少要给一个" in out and CALLS == [])

reset(EMPTY)
execute("search_place", '{"types": "050000"}')
check("只给 types 也能搜", calls_to("/place/text")[0].get("types") == "050000")

reset(POIS)
out = execute("search_place", '{"keywords": ["春熙路"]}')
check("参数类型不对时被 registry 兜住", "工具执行失败" in out, out[:36])


# ============================================================
# 5. 驾车路线
# ============================================================
section("plan_route：驾车（坐标直接算路）")
reset(DRIVING)
out = execute("plan_route",
              '{"origin": "104.081234,30.657123", "destination": "103.95,30.58"}')
p = calls_to("/direction/driving")[0] if called("/direction/driving") == 1 else {}
check("坐标算路不额外搜 POI", called("/place/text") == 0 and called("/direction/driving") == 1)
check("show_fields=cost（漏了就没有耗时）", p.get("show_fields") == "cost")
check("origin/destination 原样透传",
      p.get("origin") == "104.081234,30.657123" and p.get("destination") == "103.95,30.58")
check("距离与耗时", "距离 12.3 公里" in out and "耗时约 25 分钟" in out)
check("过路费/收费路段/红绿灯",
      "过路费 15 元" in out and "收费路段 3.0 公里" in out and "红绿灯 23 个" in out)
check("不限行（restriction=0）时不乱下结论", "限行" not in out)
check("带上打车估价（route.taxi_cost）", "打车约 21 元" in out)
check("列出前 8 段并说明总段数",
      "第1段" in out and "第8段" in out and "第9段" not in out and "共 10 段" in out)
check("标题带方式与起终点", out.startswith("驾车路线："), out.splitlines()[0])

NO_COST = {"status": "1", "info": "OK", "infocode": "10000",
           "route": {"paths": [{"distance": "12345",
                                "steps": [{"instruction": "向西", "step_distance": "200"}]}]}}

reset(DRIVING_LIMITED)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "103.95,30.58",'
                            ' "plate": "川A12345"}')
check("限行（restriction=1）时不吹「已规避」，只提醒确认",
      "限行" in out and "建议再确认" in out and "已规避" not in out, out.splitlines()[1])
reset(NO_COST)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "103.95,30.58"}')
check("响应没有 cost 时不编耗时（只给距离）",
      "距离 12.3 公里" in out and "耗时" not in out)

reset(DRIVING)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "103.95,30.58",'
                            ' "mode": "walking", "strategy": 1}')
check("步行走 walking 接口且带 strategy",
      called("/direction/walking") == 1 and calls_to("/direction/walking")[0].get("strategy") == 1)
check("步行标题正确且不显示红绿灯",
      out.startswith("步行路线：") and "红绿灯" not in out, out.splitlines()[0])

reset(DRIVING)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "103.95,30.58",'
                            ' "mode": "flying"}')
check("不支持的方式直接回报", "不支持的方式" in out and CALLS == [])

reset(DRIVING)
execute("plan_route", '{"origin": "104.08,30.65", "destination": "103.95,30.58",'
                      ' "waypoints": "104.07,30.60;104.06,30.61", "plate": "川A12345"}')
p = calls_to("/direction/driving")[0]
check("waypoints / plate 传给高德",
      p.get("waypoints") == "104.07,30.60;104.06,30.61" and p.get("plate") == "川A12345")


# ============================================================
# 6. 地名换坐标
# ============================================================
section("plan_route：地名先搜 POI 换坐标")
reset(POIS, POIS, DRIVING)
out = execute("plan_route",
              '{"origin": "成都东站", "destination": "双流机场", "city": "成都"}')
searches = calls_to("/place/text")
check("两个地名各搜一次且限定城市",
      len(searches) == 2 and all(p.get("city_limit") == "true" for p in searches))
check("解析地名只要 1 条结果", all(p.get("page_size") == 1 for p in searches))
check("算路用搜到的坐标",
      calls_to("/direction/driving")[0]["origin"] == "104.081234,30.657123")
check("标题用地点名而不是坐标",
      "春熙路" in out and "104.081234" not in out.splitlines()[0], out.splitlines()[0])

reset(POIS, EMPTY, DRIVING)
out = execute("plan_route",
              '{"origin": "成都东站", "destination": "不存在的地方xyz", "city": "成都"}')
check("终点地名搜不到：明说没找到且不硬算路",
      out.startswith("终点有问题") and "没找到地点" in out and called("/direction/driving") == 0,
      out[:40])

reset(EMPTY, DRIVING)
out = execute("plan_route", '{"origin": "不存在的地方xyz", "destination": "104.08,30.65"}')
check("起点地名搜不到只说起点",
      out.startswith("起点有问题") and called("/direction/driving") == 0)

reset(POIS, DRIVING)
execute("plan_route", '{"origin": "成都东站", "destination": "104.08,30.65", "city": "成都"}')
check("只有地名那端才搜 POI", called("/place/text") == 1)

reset(POIS, DRIVING)
execute("plan_route", '{"origin": " 104.081234，30.657123 ", "destination": "104.08,30.65"}')
check("全角逗号与空格被规整（不当地名去搜）",
      called("/place/text") == 0
      and calls_to("/direction/driving")[0]["origin"] == "104.081234,30.657123")


# ============================================================
# 7. 公交换乘
# ============================================================
section("plan_route：公交换乘")
reset(TRANSIT)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60",'
                            ' "mode": "transit"}')
check("没给 city：明确要求（公交必须城市）", "公交换乘需要 city" in out and CALLS == [])

reset(POIS, TRANSIT)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60",'
                            ' "mode": "transit", "city": "成都"}')
searches = calls_to("/place/text")
p = calls_to("/transit/integrated")[0] if called("/transit/integrated") == 1 else {}
check("中文城市名先换成 citycode（公交不认中文名）",
      len(searches) == 1 and searches[0].get("keywords") == "成都")
check("走公交接口并传 city1/city2=028", p.get("city1") == "028" and p.get("city2") == "028")
check("同城时只解析一次城市（city2 复用）", called("/place/text") == 1)
check("只取一条推荐方案（AlternativeRoute=1）", p.get("AlternativeRoute") in (1, "1"))
check("距离/耗时/票价/步行",
      "距离 8.5 公里" in out and "耗时约 40 分钟" in out
      and "票价 4 元" in out and "全程步行 900 米" in out)
check("换乘明细：步行 + 线路 + 上下车站 + 站数",
      "步行 300 米" in out and "约 4 分钟" in out and "地铁2号线" in out
      and "春熙路 → 天府广场" in out and "2 站" in out)
check("明细编号按输出行递增（同一段里步行+乘车是两行，别都编 1）",
      "   1. 步行 300 米" in out and "   2. 乘坐 地铁2号线" in out
      and "   3. 步行 500 米" in out)

reset(TRANSIT)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60",'
                            ' "mode": "transit", "city": "028"}')
p = calls_to("/transit/integrated")[0]
check("城市直接给 citycode：不额外搜 POI", called("/place/text") == 0 and p.get("city1") == "028")

reset(POIS, POIS_CQ, TRANSIT)
execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60",'
                      ' "mode": "transit", "city": "成都", "city2": "重庆"}')
p = calls_to("/transit/integrated")[0]
check("跨城时 city2 走自己的解析", p.get("city1") == "028" and p.get("city2") == "023",
      f"city1={p.get('city1')} city2={p.get('city2')}")

reset(EMPTY, TRANSIT)
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60",'
                            ' "mode": "transit", "city": "不存在的地名xyz"}')
check("城市解析不出来：明说并给出可用的写法",
      out.startswith("公交起点城市有问题") and "citycode" in out
      and called("/transit/integrated") == 0, out[:60])

reset(POIS, {"status": "1", "info": "OK", "infocode": "10000", "route": {"transits": []}})
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.085,30.655",'
                            ' "mode": "transit", "city": "成都"}')
check("没有换乘方案时说明原因", "没有可用的公交换乘方案" in out)

reset({"status": "1", "info": "OK", "infocode": "10000", "route": {"paths": []}})
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.085,30.655"}')
check("没有可达路线时说明原因", "没有可用的路线" in out)


# ============================================================
# 8. 失败与降级
# ============================================================
section("失败与降级")
reset({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
out = execute("search_place", '{"keywords": "春熙路"}')
check("Key 无效：说清原因并劝停",
      "10001" in out and "Key 不正确" in out and "重试没用" in out, out[:56])

reset({"status": "0", "info": "ACCESS_TOO_FREQUENT", "infocode": "10004"})
out = execute("plan_route", '{"origin": "104.08,30.65", "destination": "104.05,30.60"}')
check("限流：说「稍等」而不是「重试没用」",
      "稍等一下再试" in out and "重试没用" not in out, out[:52])

reset({"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"})
out = execute("search_place", '{"keywords": "春熙路"}')
check("日配额用尽：同样劝停", "今日调用量已达上限" in out and "重试没用" in out)

reset(real_requests.Timeout("timed out"))
out = execute("search_place", '{"keywords": "春熙路"}')
check("超时：明确说超时", "超时" in out, out[:36])

reset(real_requests.ConnectionError("connection refused"))
out = execute("search_place", '{"keywords": "春熙路"}')
check("连不上：说明请求失败", "请求失败" in out, out[:44])

reset(({}, 502))
out = execute("search_place", '{"keywords": "春熙路"}')
check("HTTP 5xx：说明请求失败", "请求失败" in out, out[:44])

reset("<<html>><error>502</error>")
out = execute("search_place", '{"keywords": "春熙路"}')
check("返回的不是 JSON：说明可能被网关拦了", "不是 JSON" in out, out[:44])

out = execute("search_place", "{不是 JSON")
check("模型给坏 JSON：registry 兜住且不执行", "不是合法 JSON" in out, out[:44])


# ============================================================
# 汇总
# ============================================================
print()
if FAILS:
    print(f"没通过 {len(FAILS)} 条：")
    for name in FAILS:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
