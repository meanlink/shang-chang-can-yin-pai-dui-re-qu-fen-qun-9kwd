"""corrections 端到端编排测试（内存假库）。

用最小 FakeCursor 承载 corrections.py 使用的固定 SQL 集合，驱动真实的
submit_correction / process_recompute，验证状态机、热区行复用与失败保留旧版。

运行：python3 -I tests/test_correction_flow.py
"""
import copy
import sys
import types
from datetime import datetime
from pathlib import Path
from statistics import median

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

pydantic = types.ModuleType('pydantic')
pydantic.BaseModel = type('BaseModel', (), {})
pydantic.Field = lambda *a, **k: None
pydantic.field_validator = lambda *a, **k: (lambda f: classmethod(f))
pydantic.ConfigDict = lambda **k: dict(k)
sys.modules['pydantic'] = pydantic
fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


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

from src.corrections import process_recompute, submit_correction  # noqa: E402

HOUR = datetime(2026, 11, 21, 12)


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f'PASS {name}')


class Cursor:
    def __init__(self, repo):
        self.repo = repo
        self._result = []
        self._autoid = 1000
        self._savepoints = {}

    def execute(self, sql, params=()):
        r = self.repo
        s = ' '.join(sql.split())
        if s.startswith('SELECT * FROM floor_hours WHERE floor='):
            self._result = [h for h in r['floor_hours']
                            if h['floor'] == params[0] and h['hour_start'] == params[1]]
        elif s.startswith('SELECT id,floor FROM stores WHERE id='):
            self._result = [dict(st) for st in r['stores'] if st['id'] == params[0]]
        elif s.startswith('SELECT id,queue_count FROM observations WHERE hour_id='):
            self._result = [dict(o) for o in r['observations']
                            if o['hour_id'] == params[0] and o['store_id'] == params[1]
                            and o['window_start'] == params[2]]
        elif s.startswith('SELECT id,status,old_count FROM observation_corrections'):
            self._result = [dict(c) for c in r['corrections']
                            if c['hour_id'] == params[0] and c['store_id'] == params[1]]
        elif s.startswith('SELECT h.id,h.anchor_store_id FROM hotspots h'):
            self._result = [dict(h) for h in r['hotspots']
                            if h['hour_id'] == params[0] and h['status'] == 'confirmed']
        elif s.startswith('SELECT hotspot_id,store_id FROM analysis_points'):
            self._result = [dict(p) for p in r['analysis_points']
                            if p['hour_id'] == params[0] and p['hotspot_id'] is not None]
        elif s.startswith('SELECT s.* FROM analysis_points p JOIN stores'):
            ids = {p['store_id'] for p in r['analysis_points'] if p['hour_id'] == params[0]}
            self._result = [dict(st) for st in r['stores'] if st['id'] in ids]
        elif s.startswith('SELECT store_id,window_start,queue_count FROM observations'):
            self._result = [dict(o) for o in r['observations'] if o['hour_id'] == params[0]]
        elif s.startswith('SELECT id,zone_number,anchor_store_id,status FROM hotspots'):
            self._result = [dict(h) for h in r['hotspots'] if h['hour_id'] == params[0]]
        elif s.startswith('SELECT id FROM hotspots WHERE hour_id='):
            self._result = [dict(h) for h in r['hotspots']
                            if h['hour_id'] == params[0] and h['status'] == 'proposed']
        elif s.startswith('SELECT id FROM observation_corrections WHERE hour_id='):
            self._result = [dict(c) for c in r['corrections']
                            if c['hour_id'] == params[0] and c['status'] == 'pending']
        elif s.startswith('INSERT INTO observation_corrections'):
            row = {'id': self._nextid(), 'hour_id': params[0], 'store_id': params[1],
                   'window_start': params[2], 'old_count': params[3], 'new_count': params[4],
                   'reason': params[5], 'status': 'pending', 'result_note': None,
                   'submitted_by': params[6]}
            r['corrections'].append(row)
            self.lastrowid = row['id']
        elif s.startswith('INSERT INTO hotspots'):
            row = {'id': self._nextid(), 'hour_id': params[0], 'zone_number': params[1],
                   'anchor_store_id': params[2], 'status': 'proposed', 'confirmed_by': None}
            r['hotspots'].append(row)
            self.lastrowid = row['id']
        elif s.startswith('UPDATE observation_corrections SET window_start='):
            row = next(c for c in r['corrections'] if c['id'] == params[4])
            row.update(window_start=params[0], old_count=params[1], new_count=params[2], reason=params[3])
        elif s.startswith("UPDATE floor_hours SET status='recomputing'"):
            next(h for h in r['floor_hours'] if h['id'] == params[0])['status'] = 'recomputing'
        elif s.startswith("UPDATE floor_hours SET status='ready'"):
            next(h for h in r['floor_hours'] if h['id'] == params[0])['status'] = 'ready'
        elif s.startswith('UPDATE observations SET queue_count='):
            next(o for o in r['observations'] if o['id'] == params[1])['queue_count'] = params[0]
        elif s.startswith('UPDATE analysis_points SET hotspot_id=NULL'):
            nulled = set(params)
            for point in r['analysis_points']:
                if point['hotspot_id'] in nulled:
                    point['hotspot_id'] = None
        elif s.startswith('UPDATE observation_corrections SET status='):
            note, correction_id = params
            next(c for c in r['corrections'] if c['id'] == correction_id).update(
                status='applied', result_note=note)
        elif s.startswith('DELETE FROM hotspots WHERE id IN'):
            removed = set(params)
            r['hotspots'] = [h for h in r['hotspots'] if h['id'] not in removed]
        elif s.startswith('SAVEPOINT'):
            self._savepoints[sql] = copy.deepcopy(r)
        elif s.startswith('ROLLBACK TO SAVEPOINT'):
            saved = self._savepoints.get(sql.replace('ROLLBACK TO SAVEPOINT', 'SAVEPOINT'))
            if saved is not None:
                r.clear()
                r.update(saved)
        elif s.startswith('INSERT INTO audit_events'):
            r['audit'].append(params)
        else:
            raise AssertionError(f'未覆盖的 SQL: {s}')

    def executemany(self, sql, seq):
        if sql.startswith('UPDATE analysis_points SET windows='):
            for windows, med, kind, hotspot_id, hour_id, store_id in seq:
                point = next(p for p in self.repo['analysis_points']
                             if p['hour_id'] == hour_id and p['store_id'] == store_id)
                point.update(windows=windows, median_queue=med, kind=kind, hotspot_id=hotspot_id)
        elif sql.startswith('UPDATE observation_corrections SET status='):
            for params in seq:
                self.execute(sql, params)
        else:
            raise AssertionError(f'未覆盖的 executemany: {sql}')

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def _nextid(self):
        self._autoid += 1
        return self._autoid


class Correction:
    def __init__(self, floor, hour_start, store_id, window_start, new_count, reason):
        self.floor = floor
        self.hour_start = hour_start
        self.store_id = store_id
        self.window_start = window_start
        self.new_count = new_count
        self.reason = reason


DEFAULT_STORES = [
    {'id': 1, 'code': 'F3-301', 'name': '山岚汤面', 'floor': 3, 'x': 24, 'y': 24},
    {'id': 2, 'code': 'F3-302', 'name': '禾野煲仔', 'floor': 3, 'x': 31, 'y': 24},
    {'id': 3, 'code': 'F3-303', 'name': '南屿米粉', 'floor': 3, 'x': 40, 'y': 24},
    {'id': 4, 'code': 'F3-304', 'name': '小满甜品', 'floor': 3, 'x': 56, 'y': 24},
    {'id': 5, 'code': 'F3-305', 'name': '桥边烤肉', 'floor': 3, 'x': 98, 'y': 47},
    {'id': 6, 'code': 'F3-306', 'name': '晴茶快闪', 'floor': 3, 'x': 33, 'y': 33},
    {'id': 7, 'code': 'F3-307', 'name': '青禾便当', 'floor': 3, 'x': 78, 'y': 18},
]
DEFAULT_COUNTS = {1: [12, 15, 17, 14], 2: [21, 24, 20, 23], 3: [10, 13, 15, 12],
                  4: [9, 10, 12, 11], 5: [13, 14, 16, 15], 6: [20, 22], 7: [2, 4, 4, 6]}


def build_repo(*, confirmed, stores=None, counts=None):
    custom = stores is not None or counts is not None
    stores = stores or DEFAULT_STORES
    counts = counts or DEFAULT_COUNTS
    observations = []
    obs_id = 1
    for store_id, values in counts.items():
        for index, value in enumerate(values):
            observations.append({'id': obs_id, 'hour_id': 1, 'store_id': store_id,
                                 'window_start': HOUR.replace(minute=index * 15),
                                 'queue_count': value, 'recorded_by': 1})
            obs_id += 1
    if custom:
        # 合成场景：所有达标门店同属一个群，锚点取中位数最高、同值按编号。
        eligible = {sid for sid in counts if len(counts[sid]) >= 3 and median(counts[sid]) >= 8}
        points = []
        for store_id in sorted(counts):
            values = counts[store_id]
            med = median(values) if len(values) >= 3 else None
            kind = ('insufficient' if len(values) < 3 else 'below_threshold' if med < 8 else 'core')
            points.append({'hour_id': 1, 'store_id': store_id,
                           'hotspot_id': 50 if store_id in eligible else None,
                           'windows': len(values), 'median_queue': med, 'kind': kind})
        top = max(median(counts[sid]) for sid in eligible)
        anchor = min((sid for sid in eligible if median(counts[sid]) == top),
                     key=lambda sid: next(s['code'] for s in stores if s['id'] == sid))
    else:
        # 与真实 density.map_floor 对种子数据的输出保持一致。
        kinds = {1: 'core', 2: 'core', 3: 'core', 4: 'border', 5: 'noise',
                 6: 'insufficient', 7: 'below_threshold'}
        medians = {1: 14.5, 2: 22.0, 3: 12.5, 4: 10.5, 5: 14.5, 6: None, 7: 4.0}
        points = [{'hour_id': 1, 'store_id': sid,
                   'hotspot_id': 50 if sid in (1, 2, 3, 4) else None,
                   'windows': len(counts[sid]), 'median_queue': medians[sid], 'kind': kinds[sid]}
                  for sid in sorted(counts)]
        anchor = 2
    hotspots = [{'id': 50, 'hour_id': 1, 'zone_number': 1, 'anchor_store_id': anchor,
                 'status': 'confirmed' if confirmed else 'proposed', 'confirmed_by': 9}]
    return {'floor_hours': [{'id': 1, 'floor': 3, 'hour_start': HOUR, 'status': 'ready'}],
            'stores': stores, 'observations': observations, 'analysis_points': points,
            'hotspots': hotspots, 'corrections': [], 'audit': []}


def test_safe_correction_end_to_end():
    repo = build_repo(confirmed=True)
    cur = Cursor(repo)
    data = Correction(3, HOUR, 1, HOUR, 13, '巡场复核纸质时点表后更正')
    result = submit_correction(cur, data, {'id': 9})
    check('受理后进入重算中', result['hour_status'] == 'recomputing'
          and repo['floor_hours'][0]['status'] == 'recomputing')
    check('受理时观测已改为新值', next(o for o in repo['observations'] if o['id'] == 1)['queue_count'] == 13)
    check('只产生一条更正记录', len(repo['corrections']) == 1 and repo['corrections'][0]['old_count'] == 12)

    ok = process_recompute(cur, 1)
    check('重算成功', ok and repo['floor_hours'][0]['status'] == 'ready')
    zone = next(h for h in repo['hotspots'] if h['id'] == 50)
    check('已派单热区行与锚点保留', zone['anchor_store_id'] == 2 and zone['status'] == 'confirmed')
    members = {p['store_id'] for p in repo['analysis_points'] if p['hotspot_id'] == 50}
    check('已派单热区成员不变', members == {1, 2, 3, 4})
    check('更正标记为已生效', repo['corrections'][0]['status'] == 'applied'
          and repo['corrections'][0]['result_note'])
    check('分析点中位数已重算', next(p for p in repo['analysis_points'] if p['store_id'] == 1)
          ['median_queue'] == 14.5)


def test_rejected_correction_leaves_no_record():
    # 合成三店同群：A、C 中位 11，B 中位 12 为锚；把 B 的一个 13 改为 0 后中位降到 11，
    # 按编号规则锚点改判 A，破坏已派单热区，整笔更正必须退回。
    stores = [
        {'id': 11, 'code': 'SA', 'name': '甲', 'floor': 3, 'x': 0, 'y': 0},
        {'id': 12, 'code': 'SB', 'name': '乙', 'floor': 3, 'x': 8, 'y': 0},
        {'id': 13, 'code': 'SC', 'name': '丙', 'floor': 3, 'x': 16, 'y': 0},
    ]
    counts = {11: [11, 11, 11, 11], 12: [11, 11, 13, 13], 13: [10, 10, 10, 10]}
    repo = build_repo(confirmed=True, stores=stores, counts=counts)
    check('构造场景原锚点为乙店', repo['hotspots'][0]['anchor_store_id'] == 12)
    cur = Cursor(repo)
    data = Correction(3, HOUR, 12, HOUR.replace(minute=30), 0, '错误试点更正')
    try:
        submit_correction(cur, data, {'id': 9})
    except HTTPException as error:
        check('破坏已派单热区时退回(409)', error.status_code == 409)
    else:
        raise AssertionError('应当退回')
    check('退回不新增更正记录', len(repo['corrections']) == 0)
    target = next(o for o in repo['observations']
                  if o['store_id'] == 12 and o['window_start'] == HOUR.replace(minute=30))
    check('退回不改观测值', target['queue_count'] == 13)
    check('退回后仍为已封存', repo['floor_hours'][0]['status'] == 'ready')
    check('退回后原热区与派单保留', len(repo['hotspots']) == 1 and repo['hotspots'][0]['id'] == 50)


def test_failure_keeps_previous_version_and_retry_reuses_row():
    repo = build_repo(confirmed=False)
    cur = Cursor(repo)
    data = Correction(3, HOUR, 1, HOUR, 13, '需要重试的更正')
    submit_correction(cur, data, {'id': 9})
    correction_id = repo['corrections'][0]['id']

    original_execute = cur.execute

    def failing_execute(sql, params=()):
        # 注入到重算收尾必定执行的状态更新，模拟 worker 中途失败。
        if sql.startswith("UPDATE floor_hours SET status='ready',generated_at=NOW()"):
            raise RuntimeError('模拟 worker 写入失败')
        return original_execute(sql, params)

    cur.execute = failing_execute
    ok = process_recompute(cur, 1)
    cur.execute = original_execute
    check('重算失败返回 False', ok is False)
    check('失败后恢复 ready 且旧版保留', repo['floor_hours'][0]['status'] == 'ready')
    check('失败后更正仍待处理且只有一条', len(repo['corrections']) == 1
          and repo['corrections'][0]['status'] == 'pending')
    check('旧版热区行保留', next(h for h in repo['hotspots'] if h['id'] == 50)['anchor_store_id'] == 2)

    # 用相同正确值重试：复用同一行，不新增记录。
    retry = submit_correction(cur, data, {'id': 9})
    check('重试复用原更正记录', retry['id'] == correction_id and len(repo['corrections']) == 1)
    check('重试再次进入重算中', repo['floor_hours'][0]['status'] == 'recomputing')
    check('重试成功并生效', process_recompute(cur, 1)
          and repo['corrections'][0]['status'] == 'applied')


def test_correction_blocked_when_not_ready():
    repo = build_repo(confirmed=True)
    repo['floor_hours'][0]['status'] = 'recomputing'
    cur = Cursor(repo)
    try:
        submit_correction(cur, Correction(3, HOUR, 1, HOUR, 13, '重算中再提'), {'id': 9})
    except HTTPException as error:
        check('重算中拒绝新更正', error.status_code == 409)
    else:
        raise AssertionError('应当拒绝')


if __name__ == '__main__':
    test_safe_correction_end_to_end()
    test_rejected_correction_leaves_no_record()
    test_failure_keeps_previous_version_and_retry_reuses_row()
    test_correction_blocked_when_not_ready()
    print('ALL FLOW TESTS PASSED')
