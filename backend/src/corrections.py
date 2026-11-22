from datetime import timedelta

from fastapi import HTTPException

from .density import map_floor
from .storage import audit

APPLIED_NOTE = '重算完成，未改动已派单热区'


def _load_inputs(cur, hour_id):
    cur.execute('SELECT s.* FROM analysis_points p JOIN stores s ON s.id=p.store_id '
                'WHERE p.hour_id=%s', (hour_id,))
    stores = cur.fetchall()
    cur.execute('SELECT store_id,window_start,queue_count FROM observations WHERE hour_id=%s', (hour_id,))
    return stores, cur.fetchall()


def _zone_members(cur, hour_id):
    cur.execute('SELECT hotspot_id,store_id FROM analysis_points WHERE hour_id=%s AND hotspot_id IS NOT NULL',
                (hour_id,))
    grouped = {}
    for row in cur.fetchall():
        grouped.setdefault(row['hotspot_id'], set()).add(row['store_id'])
    return grouped


def _revised_clusters(stores, observations, store_id, window_start, new_count):
    revised = [dict(row) for row in observations
               if not (row['store_id'] == store_id and row['window_start'] == window_start)]
    revised.append({'store_id': store_id, 'queue_count': new_count})
    points, anchors = map_floor(stores, revised)
    zones = {}
    for store_pk, point in points.items():
        if point['zone'] is not None:
            zones.setdefault(point['zone'], {'anchor': anchors[point['zone']], 'members': set()})
            zones[point['zone']]['members'].add(store_pk)
    return list(zones.values())


def find_guard_violation(confirmed_zones, revised_zones):
    """已派单热区在新分群中必须能找到锚点与成员完全一致的对应群，否则返回首个被破坏的热区。"""
    for old in confirmed_zones:
        preserved = any(zone['anchor'] == old['anchor'] and zone['members'] == old['members']
                        for zone in revised_zones)
        if not preserved:
            return old
    return None


def plan_zone_reuse(confirmed_rows, current_members, new_zones, used_numbers):
    """纯函数：决定每个新群复用哪个已派单热区，或分配哪个新热区编号。"""
    mapping = {}
    used = set(used_numbers)
    for zone in sorted(new_zones):
        spec = new_zones[zone]
        kept = next((row for row in confirmed_rows
                     if row['anchor_store_id'] == spec['anchor']
                     and current_members.get(row['id'], set()) == spec['members']), None)
        if kept:
            mapping[zone] = ('keep', kept['id'])
            continue
        number = next(candidate for candidate in range(1, len(used) + 2) if candidate not in used)
        used.add(number)
        mapping[zone] = ('new', number)
    return mapping


def _protect_dispatched(cur, hour_id, store_id, window_start, new_count):
    """预检查：重算不得改变任何已派单热区的成员或锚点，否则整笔更正退回。"""
    cur.execute('SELECT h.id,h.anchor_store_id FROM hotspots h WHERE h.hour_id=%s AND h.status=\'confirmed\' '
                'FOR UPDATE', (hour_id,))
    confirmed_rows = cur.fetchall()
    if not confirmed_rows:
        return
    current_members = _zone_members(cur, hour_id)
    stores, observations = _load_inputs(cur, hour_id)
    revised = _revised_clusters(stores, observations, store_id, window_start, new_count)
    confirmed = [{'anchor': row['anchor_store_id'], 'members': current_members.get(row['id'], set())}
                 for row in confirmed_rows]
    if find_guard_violation(confirmed, revised) is not None:
        raise HTTPException(409, '重算会改动已派单热区的成员或锚点，更正已退回，原疏导单照常处理')


def submit_correction(cur, data, user):
    cur.execute('SELECT * FROM floor_hours WHERE floor=%s AND hour_start=%s FOR UPDATE',
                (data.floor, data.hour_start))
    hour = cur.fetchone()
    if not hour:
        raise HTTPException(404, '楼层小时不存在')
    if hour['status'] != 'ready':
        raise HTTPException(409, '只有已封存且不在重算中的小时可以更正观测')
    cur.execute('SELECT id,floor FROM stores WHERE id=%s', (data.store_id,))
    store = cur.fetchone()
    if not store or store['floor'] != data.floor:
        raise HTTPException(404, '该楼层没有此门店')
    if not (data.hour_start <= data.window_start < data.hour_start + timedelta(hours=1)):
        raise HTTPException(422, '更正窗口须属于所选楼层小时')
    cur.execute('SELECT id,queue_count FROM observations WHERE hour_id=%s AND store_id=%s AND window_start=%s',
                (hour['id'], data.store_id, data.window_start))
    observation = cur.fetchone()
    if not observation:
        raise HTTPException(404, '该观测窗口不存在，无法更正')
    cur.execute('SELECT id,status,old_count FROM observation_corrections WHERE hour_id=%s AND store_id=%s '
                'FOR UPDATE', (hour['id'], data.store_id))
    existing = cur.fetchone()
    if existing and existing['status'] != 'pending':
        raise HTTPException(409, '该门店本小时的更正已处理，不能再次更正')
    # 对照原封存值判断；重算失败后用相同正确值重试仍可重新入队，且复用同一行更正记录。
    sealed_count = existing['old_count'] if existing else observation['queue_count']
    if sealed_count == data.new_count:
        raise HTTPException(409, '更正后的人数与原封存记录一致，无需更正')
    # 先做保护预检查，任何写入之前退回都不会留下记录。
    _protect_dispatched(cur, hour['id'], data.store_id, data.window_start, data.new_count)
    if existing:
        # 重试复用同一行，只更新更正内容，不新增更正记录。
        cur.execute('UPDATE observation_corrections SET window_start=%s,old_count=%s,new_count=%s,reason=%s '
                    'WHERE id=%s',
                    (data.window_start, sealed_count, data.new_count, data.reason, existing['id']))
        correction_id = existing['id']
    else:
        cur.execute('INSERT INTO observation_corrections(hour_id,store_id,window_start,old_count,new_count,reason,'
                    'submitted_by) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (hour['id'], data.store_id, data.window_start, sealed_count, data.new_count,
                     data.reason, user['id']))
        correction_id = cur.lastrowid
    cur.execute('UPDATE observations SET queue_count=%s WHERE id=%s', (data.new_count, observation['id']))
    cur.execute("UPDATE floor_hours SET status='recomputing' WHERE id=%s", (hour['id'],))
    audit(cur, user['id'], 'observation.correction_submitted', correction_id)
    return {'id': correction_id, 'status': 'pending', 'hour_status': 'recomputing'}


def list_corrections(cur, hour_id):
    cur.execute('SELECT c.*,s.name AS store_name,s.code AS store_code,u.username AS submitter '
                'FROM observation_corrections c JOIN stores s ON s.id=c.store_id '
                'JOIN users u ON u.id=c.submitted_by WHERE c.hour_id=%s ORDER BY c.id', (hour_id,))
    return cur.fetchall()


def _recompute(cur, hour_id):
    stores, observations = _load_inputs(cur, hour_id)
    points, anchors = map_floor(stores, observations)

    new_zones = {}
    for store_pk, point in points.items():
        if point['zone'] is not None:
            new_zones.setdefault(point['zone'], {'anchor': anchors[point['zone']], 'members': set()})
            new_zones[point['zone']]['members'].add(store_pk)

    cur.execute('SELECT id,zone_number,anchor_store_id,status FROM hotspots WHERE hour_id=%s FOR UPDATE',
                (hour_id,))
    hotspots = cur.fetchall()
    current_members = _zone_members(cur, hour_id)
    confirmed_rows = [row for row in hotspots if row['status'] == 'confirmed']
    used_numbers = {row['zone_number'] for row in hotspots}
    plan = plan_zone_reuse(confirmed_rows, current_members, new_zones, used_numbers)

    zone_id = {}
    for zone, (action, value) in plan.items():
        if action == 'keep':
            zone_id[zone] = value
            continue
        cur.execute('INSERT INTO hotspots(hour_id,zone_number,anchor_store_id) VALUES (%s,%s,%s)',
                    (hour_id, value, new_zones[zone]['anchor']))
        zone_id[zone] = cur.lastrowid

    # 未派单热区可被重算自由替换；先解除分析点引用再删除，已派单热区行始终保留。
    kept_ids = set(zone_id.values())
    cur.execute('SELECT id FROM hotspots WHERE hour_id=%s AND status=\'proposed\'', (hour_id,))
    stale = [row['id'] for row in cur.fetchall() if row['id'] not in kept_ids]
    if stale:
        placeholders = ','.join(['%s'] * len(stale))
        cur.execute(f'UPDATE analysis_points SET hotspot_id=NULL WHERE hotspot_id IN ({placeholders})', stale)
        cur.execute(f'DELETE FROM hotspots WHERE id IN ({placeholders})', stale)

    cur.executemany('UPDATE analysis_points SET windows=%s,median_queue=%s,kind=%s,hotspot_id=%s '
                    'WHERE hour_id=%s AND store_id=%s',
                    [(p['windows'], p['median_queue'], p['kind'], zone_id.get(p['zone']), hour_id, store_pk)
                     for store_pk, p in points.items()])
    cur.execute("UPDATE floor_hours SET status='ready',generated_at=NOW() WHERE id=%s", (hour_id,))
    cur.execute('SELECT id FROM observation_corrections WHERE hour_id=%s AND status=\'pending\' FOR UPDATE',
                (hour_id,))
    pending = [row['id'] for row in cur.fetchall()]
    cur.executemany('UPDATE observation_corrections SET status=\'applied\',result_note=%s WHERE id=%s',
                    [(APPLIED_NOTE, correction_id) for correction_id in pending])
    for correction_id in pending:
        audit(cur, None, 'observation.correction_applied', correction_id)
    audit(cur, None, 'shift.recomputed', hour_id)


def process_recompute(cur, hour_id):
    """在调用方的事务/保存点内执行重算。成功返回 True；失败回滚保存点并恢复 ready，返回 False。"""
    cur.execute('SAVEPOINT recompute_start')
    try:
        _recompute(cur, hour_id)
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT recompute_start')
        # 保存点回滚后封存表仍是旧版、更正行仍是 pending：旧版结果保留，重试复用该记录，不新增更正。
        cur.execute("UPDATE floor_hours SET status='ready' WHERE id=%s", (hour_id,))
        audit(cur, None, 'shift.recompute_failed', hour_id)
        return False
    return True
