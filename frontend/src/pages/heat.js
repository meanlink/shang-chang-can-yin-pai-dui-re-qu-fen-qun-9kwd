import {api, shiftQuery, state} from '../state.js';
import {badge, clock, empty, header, hourLabel, pointLabel, text} from '../ui.js';

function floorPlan(points, zones) {
  const marks = points.filter(point => point.kind !== 'insufficient');
  return `<div class="map" aria-label="排队入口平面图">
    <div class="map-caption">${state.floor}F 平面 · 120 × 64 m</div><div class="map-north">↑ 北</div>
    <div class="map-field">
      ${zones.map((zone, index) => {
        const members = marks.filter(point => point.hotspot_id === zone.id);
        const xs = members.map(point => point.x);
        const ys = members.map(point => point.y);
        const left = Math.max(0, Math.min(...xs) - 6);
        const top = Math.max(0, Math.min(...ys) - 9);
        const width = Math.min(120, Math.max(...xs) + 6) - left;
        const height = Math.min(64, Math.max(...ys) + 9) - top;
        return `<div class="map-zone" style="left:${left/1.2}%;top:${top/0.64}%;width:${width/1.2}%;height:${height/0.64}%"><span>热区 ${index + 1}</span></div>`;
      }).join('')}
      ${marks.map(point => `<div class="marker ${point.kind}" tabindex="0" data-store="${text(point.code)}" style="left:${point.x/1.2}%;top:${point.y/0.64}%" title="${text(point.name)} · ${pointLabel[point.kind]} · 中位 ${point.median_queue} 人"><i></i><span>${text(point.code.slice(-3))}</span></div>`).join('')}
    </div><div class="legend">${Object.entries(pointLabel).filter(([key]) => key !== 'insufficient').map(([key,label]) => `<span><i class="${key}"></i>${label}</span>`).join('')}</div></div>`;
}

function correctionSheet(model, observations, recomputing) {
  const role = state.session.user.role;
  const records = model.corrections || [];
  const phaseOf = item => item.status === 'applied' ? '已生效'
    : item.status === 'rejected' ? '已退回'
    : recomputing ? '重算中' : '待重试';
  const toneOf = item => item.status === 'applied' ? 'good' : item.status === 'rejected' ? 'warm' : '';
  const list = records.length ? `<div class="correction-list">${records.map(item => `<p><b>${text(item.store_name)}</b>
    <span>${clock(item.window_start)} · ${text(item.old_count)} → ${text(item.new_count)} 人</span>
    ${badge(phaseOf(item), toneOf(item))}
    <small>${text(item.result_note || item.reason)}</small></p>`).join('')}</div>` : '';
  if (role !== 'operator') {
    return `<div class="exceptions"><h3>观测更正</h3>${list || empty('暂无封存观测更正。')}</div>`;
  }
  const retryPending = records.some(item => item.status === 'pending');
  const windows = observations.map(row => ({
    value: `${row.store_id}@${row.window_start.slice(11, 16)}`,
    label: `${row.name} ${clock(row.window_start)} · 现记 ${row.queue_count} 人`,
  }));
  return `<div class="exceptions correction-panel"><h3>封存观测更正</h3>
    ${recomputing ? empty('重算进行中，页面仍显示上次封存结果；完成后自动刷新为更正后的分群。') : ''}
    ${list}
    ${recomputing ? '<button class="secondary" id="correction-refresh">刷新重算结果</button>' : `
    ${retryPending ? empty('上次重算未完成，旧版封存结果仍在显示。调整后重新提交即可重试，不会新增更正记录。') : ''}
    <form class="stack" id="correction-form">
      <label>更正窗口<select name="target" required>${windows.map(item => `<option value="${text(item.value)}">${text(item.label)}</option>`).join('')}</select></label>
      <label>正确排队人数<input name="new_count" type="number" min="0" max="300" required></label>
      <label>更正原因<input name="reason" type="text" required minlength="5" maxlength="120" placeholder="例如巡场复述时点错行，复核纸质时点表"></label>
      <button>${retryPending ? '重试更正并重算' : '提交更正并重算'}</button>
    </form><p class="minor">若重算会改动已派单热区的成员或锚点，更正将自动退回，原疏导单照常处理。已发布的交接简报保留发布时数据。</p>`}</div>`;
}

export async function heat() {
  const data = await api(`/observations?${shiftQuery()}`);
  const hour = data.hour;
  const result = hour && hour.status !== 'collecting' ? await api(`/analyses/${hour.id}`) : null;
  const status = result?.hour.status || hour?.status || 'collecting';
  const ready = status === 'ready';
  const waiting = status === 'queued';
  const recomputing = status === 'recomputing';
  const operator = state.session.user.role === 'operator';
  const short = result?.points.filter(point => point.kind === 'insufficient') || [];
  const noise = result?.points.filter(point => point.kind === 'noise') || [];
  const mapAvailable = ready || recomputing;
  const observationOptions = data.observations.map(row => ({
    ...row,
    name: data.stores.find(store => store.id === row.store_id)?.name || `门店 ${row.store_id}`,
  }));
  return {
    html: `${header('楼层运营', '热区工作台', '将短距离内持续排队的入口聚成热区。仅楼层运营可确认并下发疏导单，封存后可在下方更正观测并重算。')}
      <div class="split heat-split"><section class="panel"><div class="section-line"><h2>餐饮入口分布 ${badge(hourLabel[status], ready ? 'good' : 'warm')}
      ${recomputing ? badge('显示上次结果', 'warm') : ''}</h2>
      ${operator && !ready && !waiting && !recomputing && hour && data.observations.length ? `<button id="analyze">${result ? '重试分析' : '封存并识别热区'}</button>` : ''}
      ${waiting || recomputing ? '<button class="secondary" id="refresh">刷新结果</button>' : ''}</div>
      ${mapAvailable ? floorPlan(result.points, result.hotspots) : empty(waiting ? '后台正在计算，稍后刷新结果。' : '请在排队采样页收齐观测。完成后封存这一小时并启动分析。')}
      <div class="map-rules">${ready || recomputing ? '<a href="#handoffs">查看本小时交接简报</a>' : '<a href="#coverage">检查缺测补采</a>'}<span>邻域半径 <b>18 m</b></span><span>邻域门店 <b>至少 3 家</b></span><span>拥堵门槛 <b>中位 8 人</b></span></div></section>
      <aside class="side-note"><h2>热区和疏导点 ${ready || recomputing ? `<span class="count">${result.hotspots.length}</span>` : ''}</h2>
      ${result?.hotspots.map(zone => `<article class="area"><div class="section-line"><h3>热区 ${zone.zone_number}</h3>${badge(zone.status === 'confirmed' ? '已派单' : '待确认', zone.status === 'confirmed' ? 'good' : 'warm')}</div>
        <strong>${text(zone.staging_point)}</strong><p class="minor">锚点 ${text(zone.anchor)} · ${result.points.filter(p => p.hotspot_id === zone.id).length} 个入口</p>
        ${operator && zone.status === 'proposed' && !recomputing ? `<button data-confirm="${zone.id}">确认并下发</button>` : ''}</article>`).join('') || empty(mapAvailable ? '该时段没有符合密度条件的热区。' : '等待本楼层分析结果。')}
      ${mapAvailable ? `<div class="exceptions"><h3>样本不足 · ${short.length}</h3>${short.length ? short.map(point => `<p>${text(point.name)} <b>${point.windows}/3 窗口</b></p>`).join('') : '<p>所有门店均有至少三个窗口。</p>'}
        <h3>孤立拥堵 · ${noise.length}</h3>${noise.map(point => `<p>${text(point.name)} <b>中位 ${point.median_queue} 人</b></p>`).join('')}</div>` : ''}
      ${mapAvailable ? correctionSheet(result, observationOptions, recomputing) : ''}</aside></div>`,
    bind(root, action, refresh) {
      const analyze = root.querySelector('#analyze');
      analyze?.addEventListener('click', () => action(() => api('/analyses', {floor: state.floor, hour_start: state.hour}), '已封存，等待 worker 计算', analyze));
      root.querySelector('#refresh')?.addEventListener('click', refresh);
      root.querySelector('#correction-refresh')?.addEventListener('click', refresh);
      root.querySelectorAll('[data-confirm]').forEach(button => button.addEventListener('click', () =>
        action(() => api(`/hotspots/${button.dataset.confirm}/confirm`, {}), '热区已确认，疏导单已下发', button)));
      const form = root.querySelector('#correction-form');
      form?.addEventListener('submit', event => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(form));
        const [storeId, minutes] = values.target.split('@');
        action(() => api('/observations/corrections', {
          floor: state.floor, hour_start: state.hour, store_id: Number(storeId),
          window_start: `${state.hour.slice(0, 11)}${minutes}`, new_count: Number(values.new_count),
          reason: values.reason,
        }), '更正已受理，正在按更正后的观测重算', form);
      });
    },
  };
}
