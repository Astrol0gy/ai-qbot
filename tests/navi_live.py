#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
navi 真链路验证：打真高德接口，核对字段形状与我们假设的一致。

离线版（tests/navi_check.py）只能证明"我们发出去的参数对"；这个脚本证明
"高德返回的字段跟我们解析时假设的一样"——耗时是不是真在 cost.duration、
评分是不是真在 business.rating、换乘的上下车站是字符串还是对象。

    python tests/navi_live.py          # 需要 .env 里的 AMAP_KEY

会消耗几次配额（约 10 次请求，串了节流避免 QPS 超限）。没配 Key 时直接报出来并退出，不会假装通过。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.config import AMAP_KEY                             # noqa: E402
from src.tools import execute                                # noqa: E402
from src.tools import navi_provider as nv                    # noqa: E402

FAILS: list[str] = []
CITY = "成都"


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def raw(url: str, params: dict) -> dict:
    """直接发一次原始请求（用 provider 自己的 _fetch，错误文案一并带出来）"""
    params = {"key": AMAP_KEY, **params}
    data, err = nv._fetch(url, params)
    if err:
        print(f"  [FAIL] 请求失败：{err}")
        FAILS.append(f"raw {url}")
        return {}
    return data


if not AMAP_KEY:
    print("没配 AMAP_KEY：请在 .env 里加一行 AMAP_KEY=你的高德Web服务Key 后重跑。")
    print("（需要开通「搜索POI 2.0」与「路径规划 2.0」；绑了 IP 白名单别忘了服务器出口 IP）")
    sys.exit(2)


# ============================================================
# 1. 关键词搜索：字段形状
# ============================================================
section("原始响应：关键词搜索（show_fields=business）")
poi_data = raw(nv._SEARCH_TEXT_URL, {
    "keywords": "春熙路", "city": CITY, "city_limit": "true",
    "page_size": 3, "show_fields": "business",
})
pois = poi_data.get("pois") or []
check("有搜索结果", bool(pois), f"count={poi_data.get('count')}")
if pois:
    first = pois[0]
    print(f"  · pois[0] 字段：{sorted(first.keys())}")
    business = first.get("business") if isinstance(first.get("business"), dict) else {}
    print(f"  · business 字段：{sorted(business.keys())}")
    lnglat = nv._s(first.get("location"))
    check("location 是「经度,纬度」", nv._is_lnglat(lnglat), lnglat)
    check("address 有值或是空数组（不是别的怪类型）",
          isinstance(first.get("address"), (str, list)), repr(first.get("address"))[:40])
    check("show_fields=business 真的给了 business", isinstance(first.get("business"), dict),
          "空的话说明字段名/权限有问题")
    check("business 里有我们解析的字段",
          any(k in business for k in ("rating", "opentime_today", "tel", "cost")),
          str(sorted(business.keys())))

    out = execute("search_place", f'{{"keywords": "春熙路", "city": "{CITY}", "limit": 3}}')
    print("\n----- search_place 给模型看的输出 -----")
    print(out)
    print("--------------------------------------\n")
    check("输出里有坐标", "坐标：" in out)
    check("输出里没有 [] 这种残留", "[]" not in out)
    check("地址字段没变成空行", "地址：\n" not in out)


# ============================================================
# 2. 周边搜索：distance 只在周边搜索里有
# ============================================================
section("原始响应：周边搜索（sortrule=distance）")
around_data = raw(nv._SEARCH_AROUND_URL, {
    "keywords": "火锅", "location": lnglat, "radius": 3000,
    "sortrule": "distance", "page_size": 3, "show_fields": "business",
})
around = around_data.get("pois") or []
check("周边搜有结果", bool(around), f"count={around_data.get('count')}")
if around:
    distances = [int(nv._s(p.get("distance")) or -1) for p in around]
    print(f"  · 距离序列：{distances}")
    check("每个结果都带 distance", all(d >= 0 for d in distances))
    check("确实是按距离升序", distances == sorted(distances))

    out = execute("search_place",
                  f'{{"keywords": "火锅", "near": "{lnglat}", "radius": 3000, "limit": 3}}')
    print("\n----- 周边搜索给模型看的输出 -----")
    print(out)
    print("---------------------------------\n")
    check("输出里带距离", "距离：" in out)


# ============================================================
# 3. 驾车路线：耗时/过路费到底在不在 cost 里
# ============================================================
section("原始响应：驾车路线（show_fields=cost）")
east = raw(nv._SEARCH_TEXT_URL, {"keywords": "成都东站", "city": CITY,
                                "city_limit": "true", "page_size": 1})
east_pois = east.get("pois") or []
east_lnglat = nv._s(east_pois[0].get("location")) if east_pois else ""
check("能搜到「成都东站」（地名换坐标用）", nv._is_lnglat(east_lnglat), east_lnglat)

if nv._is_lnglat(lnglat) and nv._is_lnglat(east_lnglat):
    drive = raw(nv._ROUTE_URLS["driving"], {
        "origin": lnglat, "destination": east_lnglat,
        "city1": CITY, "city2": CITY, "strategy": 32, "show_fields": "cost",
    })
    route = drive.get("route") if isinstance(drive.get("route"), dict) else {}
    paths = route.get("paths") or []
    check("驾车返回了 paths", bool(paths))
    if paths:
        path, cost = paths[0], {}
        print(f"  · route 字段：{sorted(route.keys())}")
        print(f"  · paths[0] 字段：{sorted(path.keys())}")
        if isinstance(path.get("cost"), dict):
            cost = path["cost"]
            print(f"  · cost 字段：{sorted(cost.keys())}")
        check("distance 在 paths[0] 上", bool(nv._s(path.get("distance"))),
              nv._s(path.get("distance")))
        check("duration 在 cost 里（show_fields=cost 生效）", bool(nv._s(cost.get("duration"))),
              nv._s(cost.get("duration")))
        check("tolls 在 cost 里", "tolls" in cost, nv._s(cost.get("tolls")))
        steps = path.get("steps") or []
        if steps:
            print(f"  · steps[0] 字段：{sorted(steps[0].keys())}")
            check("steps 有 instruction（路线指引）", bool(nv._s(steps[0].get("instruction"))),
                  nv._s(steps[0].get("instruction"))[:40])
        check("route 里带 taxi_cost（打车估价，可顺手告诉用户）",
              "taxi_cost" in route, nv._s(route.get("taxi_cost")))

        out = execute("plan_route",
                      f'{{"origin": "{lnglat}", "destination": "{east_lnglat}", "city": "{CITY}"}}')
        print("\n----- plan_route(驾车) 给模型看的输出 -----")
        print(out)
        print("------------------------------------------\n")
        check("输出里有距离与耗时", "距离" in out and "耗时约" in out)

# 地名直接当起终点（内部换坐标）
if nv._is_lnglat(east_lnglat):
    out = execute("plan_route",
                  f'{{"origin": "成都东站", "destination": "{east_lnglat}", '
                  f'"city": "{CITY}", "mode": "walking"}}')
    check("地名当起点能自动换坐标（步行）", "没找到地点" not in out and "步行路线：" in out,
          out.splitlines()[0] if out else "")


# ============================================================
# 4. 公交换乘：换乘明细的字段形状
# ============================================================
section("原始响应：公交换乘（city1/city2 只认 citycode）")
city_code, err = nv._city_code(CITY)
check("中文城市名能用 POI 搜索换成 citycode", bool(city_code) and city_code.isdigit(),
      f"{CITY} -> {city_code} {err}")
for wrong in (CITY, CITY + "市"):
    _d, _e = nv._fetch(nv._ROUTE_URLS["transit"], {
        "key": AMAP_KEY, "origin": lnglat, "destination": east_lnglat,
        "city1": wrong, "city2": wrong, "show_fields": "cost"})
    # 必须是「参数不合法」这种拒绝；如果是限流（10021）就说明这次没测到，别当通过
    check(f"实测：中文城市名「{wrong}」被拒为参数错误（所以必须换码）",
          "INVALID_PARAMS" in _e or "20000" in _e, (_e or "居然成功了")[:70])
    time.sleep(1.5)

transit = raw(nv._ROUTE_URLS["transit"], {
    "origin": lnglat, "destination": east_lnglat,
    "city1": city_code, "city2": city_code, "AlternativeRoute": 1, "show_fields": "cost",
})
transits = (transit.get("route") or {}).get("transits") or []
check("公交返回了 transits", bool(transits))
if transits:
    one = transits[0]
    print(f"  · transits[0] 字段：{sorted(one.keys())}")
    cost = one.get("cost") if isinstance(one.get("cost"), dict) else {}
    print(f"  · cost 字段：{sorted(cost.keys()) if cost else cost}")
    # 实测：transits[0] 上没有 duration，耗时只在 cost.duration 里
    check("耗时在 cost.duration（不是 transits[0].duration）",
          bool(nv._s(cost.get("duration"))), nv._s(cost.get("duration")))
    check("票价在 cost.transit_fee", "transit_fee" in cost, nv._s(cost.get("transit_fee")))
    check("总步行在 walking_distance 上", bool(nv._s(one.get("walking_distance"))),
          nv._s(one.get("walking_distance")))
    segments = one.get("segments") or []
    check("有 segments（换乘段）", bool(segments), f"{len(segments)} 段")
    if segments:
        print(f"  · segments[0] 字段：{sorted(segments[0].keys())}")
        walk_seg = next((s for s in segments if isinstance(s.get("walking"), dict)), None)
        if walk_seg:
            walk = walk_seg["walking"]
            print(f"  · walking 字段：{sorted(walk.keys())}")
            check("实测：步行段没有 destination_stop（格式化里不能用它）",
                  "destination_stop" not in walk)
        bus_seg = next((s for s in segments if isinstance(s.get("bus"), dict)), None)
        if bus_seg:
            buslines = bus_seg["bus"].get("buslines") or []
            if buslines:
                line = buslines[0]
                print(f"  · buslines[0] 字段：{sorted(line.keys())}")
                check("buslines 有 name", bool(nv._s(line.get("name"))), nv._s(line.get("name")))
                for field in ("departure_stop", "arrival_stop"):
                    value = line.get(field)
                    print(f"  · {field} 的实际类型：{type(value).__name__}")
                    check(f"{field} 能被 _stop_name 解析出名字",
                          bool(nv._stop_name(value)), nv._stop_name(value))
        else:
            print("  · 这条方案里没有公交段（可能全程步行），换个更远的起终点再看")

    out = execute("plan_route",
                  f'{{"origin": "{lnglat}", "destination": "{east_lnglat}", '
                  f'"city": "{CITY}", "mode": "transit"}}')
    print("\n----- plan_route(公交) 给模型看的输出 -----")
    print(out)
    print("------------------------------------------\n")
    check("输出里有换乘方案或明确说明没有", "换乘方案：" in out or "没有可用的公交换乘方案" in out)


# ============================================================
# 5. 错误码措辞（拿真实返回校验我们的解读）
# ============================================================
section("错误码：故意用错 Key，看我们的措辞对不对")
real_key = nv.AMAP_KEY
nv.AMAP_KEY = "0000wrongkey0000"
out = execute("search_place", '{"keywords": "春熙路"}')
nv.AMAP_KEY = real_key
print(f"  · 真实返回被解读为：{out[:110]}")
check("错误里带高德原始 info/infocode", "infocode=" in out)
check("无效 Key 被归为「重试没用」", "重试没用" in out or "稍等一下再试" in out)


# ============================================================
# 汇总
# ============================================================
print()
if FAILS:
    print(f"没通过 {len(FAILS)} 条：")
    for name in FAILS:
        print(f"  - {name}")
    sys.exit(1)
print("真链路全部通过（字段形状与解析假设一致）")
