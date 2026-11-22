"""更正与重算业务规则测试。

本机没有 MySQL/Docker 与第三方依赖，运行前用最小桩模块顶替 pydantic、fastapi、
pymysql、redis，仅导入被测的纯函数与真实 DBSCAN 实现 density.map_floor。

运行：python3 -I tests/test_corrections.py
"""
import sys
import types
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# --- 依赖桩：仅满足 import 期与被测代码用到的符号 ---
pydantic = types.ModuleType('pydantic')


class BaseModel:
    pass


class Field:
    def __init__(self, *args, **kwargs):
        pass


def field_validator(*args, **kwargs):
    def decorator(func):
        return classmethod(func)
    return decorator


def ConfigDict(**kwargs):
    return dict(kwargs)


pydantic.BaseModel = BaseModel
pydantic.Field = Field
pydantic.field_validator = field_validator
pydantic.ConfigDict = ConfigDict
sys.modules['pydantic'] = pydantic

fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


fastapi.HTTPException = HTTPException
sys.modules['fastapi'] = fastapi

pymysql = types.ModuleType('pymysql')
pymysql.connect = lambda *a, **k: None
pymysql.cursors = types.SimpleNamespace(DictCursor=object)
pymysql.IntegrityError = type('IntegrityError', (Exception,), {})
pymysql.OperationalError = type('OperationalError', (Exception,), {})
sys.modules['pymysql'] = pymysql

redis_mod = types.ModuleType('redis')
redis_mod.Redis = type('Redis', (), {'__init__': lambda self, *a, **k: None})
sys.modules['redis'] = redis_mod

from src.corrections import (  # noqa: E402
    _revised_clusters,
    find_guard_violation,
    plan_zone_reuse,
)
from src.density import map_floor  # noqa: E402

SEED_STORES = [
    {'id': 1, 'code': 'F3-301', 'x': 24, 'y': 24},
    {'id': 2, 'code': 'F3-302', 'x': 31, 'y': 24},
    {'id': 3, 'code': 'F3-303', 'x': 40, 'y': 24},
    {'id': 4, 'code': 'F3-304', 'x': 56, 'y': 24},
    {'id': 5, 'code': 'F3-305', 'x': 98, 'y': 47},
    {'id': 6, 'code': 'F3-306', 'x': 33, 'y': 33},
    {'id': 7, 'code': 'F3-307', 'x': 78, 'y': 18},
]
SEED_COUNTS = {
    1: [12, 15, 17, 14], 2: [21, 24, 20, 23], 3: [10, 13, 15, 12],
    4: [9, 10, 12, 11], 5: [13, 14, 16, 15], 6: [20, 22], 7: [2, 4, 4, 6],
}
HOUR = datetime(2026, 11, 21, 12)


def seed_observations():
    rows = []
    for store_id, counts in SEED_COUNTS.items():
        for index, count in enumerate(counts):
            rows.append({'store_id': store_id,
                         'window_start': HOUR.replace(minute=index * 15),
                         'queue_count': count})
    return rows


def zones_from(points, anchors):
    zones = {}
    for store_id, point in points.items():
        if point['zone'] is not None:
            zones.setdefault(point['zone'], {'anchor': anchors[point['zone']], 'members': set()})
            zones[point['zone']]['members'].add(store_id)
    return list(zones.values())


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f'PASS {name}')


def test_baseline_clusters():
    points, anchors = map_floor(SEED_STORES, seed_observations())
    zones = zones_from(points, anchors)
    check('基线仅一个热区', len(zones) == 1)
    zone = zones[0]
    check('基线成员为三个核心点加边界点 304', zone['members'] == {1, 2, 3, 4})
    check('基线锚点为中位数最高的 302', zone['anchor'] == 2)
    check('305 为孤立噪声', points[5]['kind'] == 'noise')
    check('306 样本不足', points[6]['kind'] == 'insufficient')


def test_safe_correction_preserves_dispatched_zone():
    observations = seed_observations()
    # 301 的 12:00 由 12 改为 13：中位数微升但分群、锚点完全不变。
    revised = _revised_clusters(SEED_STORES, observations, 1, HOUR, 13)
    confirmed = [{'anchor': 2, 'members': {1, 2, 3, 4}}]
    check('不影响已派单热区的更正通过保护检查', find_guard_violation(confirmed, revised) is None)


def test_anchor_change_is_rejected():
    # 构造可由单窗口更正引发锚点易主的数据：A、B 同中位数 11，按编号 A 为锚；
    # B 原中位数 12，把 B 的一个 13 改成 0 后中位数降到 11，锚点按编号规则变为 A。
    stores = [
        {'id': 11, 'code': 'SA', 'x': 0, 'y': 0},
        {'id': 12, 'code': 'SB', 'x': 8, 'y': 0},
        {'id': 13, 'code': 'SC', 'x': 16, 'y': 0},
    ]
    counts = {11: [11, 11, 11, 11], 12: [11, 11, 13, 13], 13: [10, 10, 10, 10]}
    observations = [{'store_id': sid, 'window_start': HOUR.replace(minute=i * 15), 'queue_count': value}
                    for sid, values in counts.items() for i, value in enumerate(values)]
    points, anchors = map_floor(stores, observations)
    before = zones_from(points, anchors)
    check('构造数据原锚点为 B', before == [{'anchor': 12, 'members': {11, 12, 13}}])
    revised = _revised_clusters(stores, observations, 12, HOUR.replace(minute=30), 0)
    check('更正后锚点变为 A', revised == [{'anchor': 11, 'members': {11, 12, 13}}])
    violation = find_guard_violation(before, revised)
    check('锚点变化触发退回', violation == {'anchor': 12, 'members': {11, 12, 13}})


def test_guard_pure_rules():
    confirmed = [{'anchor': 2, 'members': {1, 2, 3}}]
    check('成员一致即通过', find_guard_violation(confirmed, [{'anchor': 2, 'members': {1, 2, 3}}]) is None)
    check('成员变化被拦截',
          find_guard_violation(confirmed, [{'anchor': 2, 'members': {1, 2}}]) is not None)
    check('锚点变化被拦截',
          find_guard_violation(confirmed, [{'anchor': 9, 'members': {1, 2, 3}}]) is not None)
    check('热区消失被拦截', find_guard_violation(confirmed, []) is not None)


def test_zone_reuse_planning():
    confirmed_rows = [
        {'id': 50, 'zone_number': 1, 'anchor_store_id': 2, 'status': 'confirmed'},
    ]
    current_members = {50: {1, 2, 3, 4}}
    new_zones = {
        1: {'anchor': 2, 'members': {1, 2, 3, 4}},       # 与已派单热区完全一致，复用
        2: {'anchor': 7, 'members': {7, 8}},             # 新群，取未占用编号
    }
    plan = plan_zone_reuse(confirmed_rows, current_members, new_zones, {1})
    check('已派单热区锚点成员一致时复用原行', plan[1] == ('keep', 50))
    check('新群分配未占用的热区编号', plan[2] == ('new', 2))

    # 已派单热区成员变化后不得复用，旧行保留，新群另取编号。
    changed = {1: {'anchor': 2, 'members': {1, 2, 3}}}
    plan_changed = plan_zone_reuse(confirmed_rows, current_members, changed, {1})
    check('成员变化时不复用已派单行', plan_changed[1] == ('new', 2))

    # 编号冲突时顺延。
    plan_gap = plan_zone_reuse([], {}, {1: {'anchor': 9, 'members': {9}}}, {1, 2})
    check('新编号跳过已占用编号', plan_gap[1] == ('new', 3))


def test_recompute_is_deterministic():
    observations = seed_observations()
    first = _revised_clusters(SEED_STORES, observations, 1, HOUR, 13)
    # 重试等价于在已更正数据上再算一次，结果必须逐字节稳定，且不依赖更正次数。
    revised_rows = [dict(row) for row in observations
                    if not (row['store_id'] == 1 and row['window_start'] == HOUR)]
    revised_rows.append({'store_id': 1, 'window_start': HOUR, 'queue_count': 13})
    points, anchors = map_floor(SEED_STORES, revised_rows)
    second = zones_from(points, anchors)
    check('重算结果可复现', first == second)


if __name__ == '__main__':
    test_baseline_clusters()
    test_safe_correction_preserves_dispatched_zone()
    test_anchor_change_is_rejected()
    test_guard_pure_rules()
    test_zone_reuse_planning()
    test_recompute_is_deterministic()
    print('ALL TESTS PASSED')
