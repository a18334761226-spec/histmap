/*!
 * histmap-web · 浏览器端渲染引擎
 * ================================
 * packages/core/histmap_core 的 JS 移植版。设计约束（见 docs/design-browser-render.md）：
 *   1. 与 Python 端**同契约**（Region / Frame / Series / ControlTimeline）
 *   2. 与 Python 端**同公式**（投影、控制层求值、标注避让），能像素级对齐
 *   3. 零依赖，只用 Canvas 2D
 *
 * 渲染策略：几何只按年变化，因此 Path2D 按「场景+年份」缓存一次；
 * 换月份只重算填充色，不重建路径。这是能做到 60fps 拖时间轴的关键。
 */

// ─────────────────────────────────────────────────────────────
// 1. 投影（对应 projection.py）
// ─────────────────────────────────────────────────────────────

export class Viewport {
  constructor(x, y, width, height) {
    this.x = x; this.y = y; this.width = width; this.height = height;
  }
}

class Projection {
  constructor(bbox, viewport, padding = 0, skipFit = false) {
    this.bbox = bbox;
    this.vp = viewport;
    this.padding = padding;
    if (!skipFit) this._fit();
  }

  _project(lon, lat) { throw new Error("子类必须实现 _project"); }

  _fit() {
    const [minLon, minLat, maxLon, maxLat] = this.bbox;
    // 与 Python 完全一致：在边界上 25×25 采样求投影后范围
    const steps = 24;
    let xs = [], ys = [];
    for (let i = 0; i <= steps; i++) {
      const lon = minLon + (maxLon - minLon) * i / steps;
      for (let j = 0; j <= steps; j++) {
        const lat = minLat + (maxLat - minLat) * j / steps;
        const [x, y] = this._project(lon, lat);
        xs.push(x); ys.push(y);
      }
    }
    this.pxMin = Math.min(...xs); this.pxMax = Math.max(...xs);
    this.pyMin = Math.min(...ys); this.pyMax = Math.max(...ys);

    const availW = this.vp.width - 2 * this.padding;
    const availH = this.vp.height - 2 * this.padding;
    const spanX = Math.max(this.pxMax - this.pxMin, 1e-9);
    const spanY = Math.max(this.pyMax - this.pyMin, 1e-9);
    this.scale = Math.min(availW / spanX, availH / spanY);

    this.offX = this.vp.x + this.padding
      + (availW - spanX * this.scale) / 2 - this.pxMin * this.scale;
    this.offY = this.vp.y + this.padding
      + (availH - spanY * this.scale) / 2 - this.pyMin * this.scale;
  }

  /** 经纬度 -> 画布像素。返回 [x, y]。 */
  at(lon, lat) {
    const [x, y] = this._project(lon, lat);
    return [x * this.scale + this.offX, y * this.scale + this.offY];
  }
}

export class Equirectangular extends Projection {
  _project(lon, lat) {
    const D = Math.PI / 180;
    return [lon * D, lat * D];
  }
}

export class Mercator extends Projection {
  _project(lon, lat) {
    const D = Math.PI / 180;
    const la = Math.max(Math.min(lat, 85.0), -85.0);
    const x = lon * D;
    const y = Math.log(Math.tan(Math.PI / 4 + (la * D) / 2));
    return [x, -y];            // 屏幕 y 向下
  }
}

export class LambertConformal extends Projection {
  constructor(bbox, viewport, padding = 0, stdParallels = null) {
    const [minLon, minLat, maxLon, maxLat] = bbox;
    if (!stdParallels) {
      stdParallels = [
        minLat + (maxLat - minLat) * 0.25,
        minLat + (maxLat - minLat) * 0.75,
      ];
    }
    const D = Math.PI / 180;
    const lat1 = stdParallels[0] * D;
    const lat2 = stdParallels[1] * D;
    const lon0 = ((minLon + maxLon) / 2) * D;
    const lat0 = ((minLat + maxLat) / 2) * D;
    let n;
    if (Math.abs(lat1 - lat2) < 1e-9) {
      n = Math.sin(lat1);
    } else {
      n = Math.log(Math.cos(lat1) / Math.cos(lat2))
        / Math.log(Math.tan(Math.PI / 4 + lat2 / 2) / Math.tan(Math.PI / 4 + lat1 / 2));
    }
    const F = (Math.cos(lat1) * Math.pow(Math.tan(Math.PI / 4 + lat1 / 2), n)) / n;
    const rho0 = F / Math.pow(Math.tan(Math.PI / 4 + lat0 / 2), n);
    // 先跳过基类的 _fit（此时 n/F/rho0 还没赋值），赋值后再手动算一次。
    // 注意 padding 必须原样传下去，写成 0 会让结果整体偏移一个 padding。
    super(bbox, viewport, padding, true);
    this.n = n; this.F = F; this.rho0 = rho0; this.lon0 = lon0;
    this._fit();
  }

  _project(lon, lat) {
    const D = Math.PI / 180;
    const la = Math.max(Math.min(lat, 89.0), -89.0) * D;
    const rho = this.F / Math.pow(Math.tan(Math.PI / 4 + la / 2), this.n);
    const theta = this.n * (lon * D - this.lon0);
    return [rho * Math.sin(theta), -(this.rho0 - rho * Math.cos(theta))];
  }
}

const PROJECTIONS = {
  equirectangular: Equirectangular,
  mercator: Mercator,
  lambert: LambertConformal,
};

export function getProjection(name) {
  return PROJECTIONS[name] || Equirectangular;
}

// ─────────────────────────────────────────────────────────────
// 2. 布局（对应 render.py 的 Layout）
// ─────────────────────────────────────────────────────────────

export class Layout {
  constructor(opts = {}) {
    this.width = opts.width ?? 1080;
    this.height = opts.height ?? 1920;
    this.marginRatio = opts.marginRatio ?? 0.045;
    this.titleRatio = opts.titleRatio ?? 0.13;
    this.footerRatio = opts.footerRatio ?? 0.06;
    this.mode = opts.mode ?? "full";
    this.bandTopRatio = opts.bandTopRatio ?? 0.22;
    this.bandMaxHeightRatio = opts.bandMaxHeightRatio ?? 0.52;
  }

  get margin() { return this.width * this.marginRatio; }

  get mapArea() {
    const top = this.height * this.titleRatio;
    const bottom = this.height * (1 - this.footerRatio);
    return new Viewport(this.margin, top,
      this.width - 2 * this.margin, bottom - top);
  }

  /** band 模式：竖版放横向地图时不硬塞，改成带状居中（避免大片留白）。 */
  mapAreaForRatio(dataRatio) {
    if (this.mode !== "band" || !dataRatio || dataRatio <= 0) return this.mapArea;
    const m = this.margin;
    let availW = this.width - 2 * m;
    let h = availW / dataRatio;
    const maxH = this.height * this.bandMaxHeightRatio;
    if (h > maxH) { h = maxH; availW = h * dataRatio; }
    const top = this.height * this.bandTopRatio;
    const left = (this.width - availW) / 2;
    return new Viewport(left, top, availW, h);
  }
}

export function bboxRatio(bbox) {
  if (!bbox) return 1.0;
  const [minLon, minLat, maxLon, maxLat] = bbox;
  const dy = Math.max(maxLat - minLat, 1e-6);
  return (maxLon - minLon) / dy;
}

// ─────────────────────────────────────────────────────────────
// 3. 控制时间线（对应 control.py 的 ControlTimeline）
// ─────────────────────────────────────────────────────────────

/** 把 {by,type} 规范化成 {control_by,control_type}（与 Python 端 _norm 一致）。 */
function normRule(rule) {
  const r = Object.assign({}, rule || {});
  if ("by" in r && !("control_by" in r)) { r.control_by = r.by; delete r.by; }
  if ("type" in r && !("control_type" in r)) { r.control_type = r.type; delete r.type; }
  return r;
}

export class ControlTimeline {
  constructor(data) {
    this.data = data || {};
    const raw = this.data._baseline_1939 || this.data._baseline || {};
    this.baseline = {};
    for (const [k, v] of Object.entries(raw)) this.baseline[k] = normRule(v);
    this.palette = this.data._palette || {};
    this.events = (this.data.events || [])
      .slice()
      .sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
    this.markers = this.data.markers || [];
  }

  /** 任意日期 -> {实体名: 规则}。 */
  stateAt(dateStr) {
    const d = String(dateStr).slice(0, 10);
    const state = {};
    for (const [k, v] of Object.entries(this.baseline)) state[k] = Object.assign({}, v);
    for (const e of this.events) {
      if (String(e.date || "").slice(0, 10) > d) break;
      const ent = e.entity;
      if (!ent) continue;
      if (e.drop) { delete state[ent]; continue; }
      const rule = Object.assign({}, state[ent] || {});
      const ev = normRule(e);
      if ("control_by" in ev) rule.control_by = ev.control_by || ent;
      if ("control_type" in ev) rule.control_type = ev.control_type;
      if ("label" in ev) rule.label = ev.label;
      state[ent] = rule;
    }
    return state;
  }

  /** 规则 -> 颜色（对应 ControlLayer.color_for）。 */
  colorFor(rule) {
    const ctype = rule.control_type || "";
    const cby = rule.control_by || "";
    const pal = this.palette;
    const entry = pal[cby];
    if (entry && typeof entry === "object" && ctype in entry) return entry[ctype];
    if (ctype in pal && typeof pal[ctype] === "string") return pal[ctype];
    if (typeof entry === "string") return entry;
    return pal.default || "#3a4048";
  }

  /** 把控制状态写进 region.props / region.color；可重复调用（幂等）。
   *  调用前必须先 setDate()。 */
  apply(frame, useCnAsName = true) {
    const st = this._state || this.stateAt(this._date || "1939-01-01");
    const stats = { matched: 0, unmatched: 0, byControl: {}, byType: {} };
    // 长键优先，避免 "France" 抢先匹配 "French Indochina"
    const sorted = Object.keys(st).sort((a, b) => b.length - a.length);

    for (const reg of frame.features) {
      reg.props = Object.assign({}, reg.props || {});
      if (!reg.props.orig_name) reg.props.orig_name = reg.name;
      const orig = reg.props.orig_name;
      // 先按实体名匹配，再按 members 逐个试（合并实体用）
      let name = null;
      for (const n of [orig, ...(reg.props.members || [])]) {
        if (!n) continue;
        if (st[n]) { name = n; break; }
        for (const k of sorted) { if (n.includes(k)) { name = k; break; } }
        if (name) break;
      }
      const rule = name ? st[name] : null;
      if (rule) {
        stats.matched++;
        reg.props.control_by = rule.control_by;
        reg.props.control_type = rule.control_type;
        reg.props.control_label = rule.label;
        if (useCnAsName && rule.cn) reg.name = rule.cn;
        reg.color = rule.color || this.colorFor(rule);
        const c = rule.control_by || "?";
        const t = rule.control_type || "?";
        stats.byControl[c] = (stats.byControl[c] || 0) + 1;
        stats.byType[t] = (stats.byType[t] || 0) + 1;
      } else {
        stats.unmatched++;
        reg.props.control_by = null;
        reg.color = this.palette.default || "#3a4048";
      }
    }
    return stats;
  }

  /** 设定当前状态（apply 前必须先调）。 */
  setDate(dateStr) {
    this._state = this.stateAt(dateStr);
    this._date = dateStr;
    return this._state;
  }

  /** (prev, cur] 内的事件与标记。 */
  beatsBetween(prev, cur) {
    const out = [];
    for (const e of this.events) {
      const d = String(e.date || "").slice(0, 10);
      if ((!prev || d > prev) && d <= cur) out.push({ date: d, label: e.label || "", kind: "event" });
    }
    for (const m of this.markers) {
      const d = String(m.date || "").slice(0, 10);
      if ((!prev || d > prev) && d <= cur) out.push({ date: d, label: m.label || "", kind: "marker" });
    }
    out.sort((a, b) => a.date.localeCompare(b.date));
    return out;
  }

  /** 截至 cur 的最近 n 条史事（含包含关系去重）。
   *  按日期从新到旧取、同一天内保持文件顺序 —— 简单反转整个列表会把
   *  重点事件挤出面板（1945-05-08 那天有 13 条）。 */
  recentLines(cur, n = 3) {
    const items = [];
    for (const e of this.events) {
      const d = String(e.date || "").slice(0, 10);
      if (d <= cur) items.push({ date: d, label: e.label || "" });
    }
    for (const m of this.markers) {
      const d = String(m.date || "").slice(0, 10);
      if (d <= cur) items.push({ date: d, label: m.label || "" });
    }
    items.sort((a, b) => a.date.localeCompare(b.date));

    const byDate = new Map();
    for (const it of items) {
      if (!byDate.has(it.date)) byDate.set(it.date, []);
      byDate.get(it.date).push(it.label);
    }
    const dates = [...byDate.keys()].sort().reverse();
    const picked = [], seen = new Set();
    for (const d of dates) {
      for (const lab of byDate.get(d)) {
        if (!lab || seen.has(lab)) continue;
        let dup = false;
        for (const s of seen) if (lab.includes(s) || s.includes(lab)) { dup = true; break; }
        if (dup) continue;
        seen.add(lab);
        picked.push({ date: d, label: lab });
      }
      if (picked.length >= n) break;
    }
    const out = picked.slice(0, n);
    // 显示时按时间正序；同一天内保持文件顺序
    const dset = [...new Set(out.map((x) => x.date))].sort();
    const res = [];
    for (const d of dset) for (const it of out) if (it.date === d) res.push(it);
    return res;
  }
}

// ─────────────────────────────────────────────────────────────
// 4. 渲染器（对应 render.py 的 Renderer）
// ─────────────────────────────────────────────────────────────

function hexToRgb(hex) {
  let h = String(hex || "#000").replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const n = parseInt(h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgba(hex, a) {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r},${g},${b},${a})`;
}

/**
 * 把样式对象规范化成扁平字段（对应 style.py 的 Style.from_dict）。
 * 交换格式只有一种 —— Style.to_dict() 输出的嵌套结构，两端共用。
 */
export function normalizeStyle(d) {
  d = d || {};
  const s = {
    id: d.id ?? "default",
    background: "#0a0e14",
    palette: [],
    colorOverrides: {},
    borderColor: "#000000",
    borderWidth: 1.0,
    regionAlpha: 1.0,
    labelSize: 22,
    labelColor: "#f5f5f5",
    labelHalo: "#000000",
    labelHaloWidth: 3,
    labelMinAreaRatio: 0.0,
    titleSize: 46,
    titleColor: "#ffffff",
    subtitleSize: 26,
    subtitleColor: "#bdc3c7",
    legend: { enabled: true, position: "bottom-left", size: 18, maxItems: 14 },
  };
  const canvas = d.canvas || {};
  if (canvas.background) s.background = canvas.background;

  const pal = d.palette || {};
  if (Array.isArray(pal)) s.palette = pal;
  else {
    if (pal.colors) s.palette = pal.colors;
    s.colorOverrides = pal.overrides || {};
  }

  const b = d.borders || {};
  if (b.color) s.borderColor = b.color;
  if (b.width !== undefined) s.borderWidth = Number(b.width);
  if (b.region_alpha !== undefined) s.regionAlpha = Number(b.region_alpha);

  const lb = d.labels || {};
  if (lb.size !== undefined) s.labelSize = Number(lb.size);
  if (lb.color) s.labelColor = lb.color;
  if (lb.halo) s.labelHalo = lb.halo;
  if (lb.halo_width !== undefined) s.labelHaloWidth = Number(lb.halo_width);
  if (lb.min_area_ratio !== undefined) s.labelMinAreaRatio = Number(lb.min_area_ratio);

  const ti = d.title_style || d.title || {};
  if (ti.size !== undefined) s.titleSize = Number(ti.size);
  if (ti.color) s.titleColor = ti.color;
  if (ti.subtitle_size !== undefined) s.subtitleSize = Number(ti.subtitle_size);
  if (ti.subtitle_color) s.subtitleColor = ti.subtitle_color;

  const lg = d.legend || {};
  s.legend = {
    enabled: lg.enabled !== undefined ? !!lg.enabled : true,
    position: lg.position || "bottom-left",
    size: lg.size !== undefined ? Number(lg.size) : 18,
    maxItems: lg.max_items !== undefined ? Number(lg.max_items) : 14,
  };
  return s;
}

function ringArea(pts) {
  if (!pts || pts.length < 3) return 0;
  let s = 0;
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[(i + 1) % n];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

function centroid(pts) {
  if (!pts || !pts.length) return [0, 0];
  let sx = 0, sy = 0;
  for (const p of pts) { sx += p[0]; sy += p[1]; }
  return [sx / pts.length, sy / pts.length];
}

function collides(box, placed) {
  const [ax1, ay1, ax2, ay2] = box;
  for (const [bx1, by1, bx2, by2] of placed) {
    if (ax1 < bx2 && ax2 > bx1 && ay1 < by2 && ay2 > by1) return true;
  }
  return false;
}

/** 跨洲实体的兜底锚点：最大环的投影包围盒 ∩ 视口，取中点。 */
function bboxAnchor(rings, vp) {
  let best = null, bestA = -1;
  for (const r of rings) {
    const a = Math.abs(ringArea(r));
    if (a > bestA) { bestA = a; best = r; }
  }
  if (!best || !best.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of best) {
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  const cx0 = Math.max(x0, vp.x), cx1 = Math.min(x1, vp.x + vp.width);
  const cy0 = Math.max(y0, vp.y), cy1 = Math.min(y1, vp.y + vp.height);
  if (cx1 <= cx0 || cy1 <= cy0) return null;
  return [(cx0 + cx1) / 2, (cy0 + cy1) / 2];
}

export class Renderer {
  /**
   * @param {object} style 样式对象（见 scenes/*.json 引用的 style）
   * @param {Layout} layout
   * @param {object} opts  { projection, supersample }
   */
  constructor(style, layout, opts = {}) {
    this.style = normalizeStyle(style);
    this.layout = layout;
    this.projectionName = opts.projection || "mercator";
    this.ss = Math.max(1, Math.floor(opts.supersample ?? 2));
    this._pathCache = new Map();
    this.debugLabels = false;
  }

  _font(size) {
    return `${size}px "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif`;
  }

  /**
   * 建/取路径缓存。几何只在 bbox / 视口变化时重算 —— 这是拖时间轴流畅的关键。
   * key 由调用方给定（通常是 `${sceneId}:${year}:${bbox}`）。
   */
  buildPaths(cacheKey, features, bbox) {
    if (this._pathCache.has(cacheKey)) return this._pathCache.get(cacheKey);
    const vp0 = this.layout.mapAreaForRatio(bboxRatio(bbox));
    const vp = new Viewport(vp0.x * this.ss, vp0.y * this.ss,
      vp0.width * this.ss, vp0.height * this.ss);
    const proj = new (getProjection(this.projectionName))(bbox, vp, 8 * this.ss);

    const items = features.map((f) => {
      const rings = f.rings.map((ring) => ring.map(([lon, lat]) => proj.at(lon, lat)));
      return { feature: f, rings, path: null };
    });
    const out = { vp, proj, items };
    this._pathCache.set(cacheKey, out);
    return out;
  }

  /** 渲染到 canvas（canvas 尺寸 = layout.width/height * ss，内部再降采样）。 */
  renderFrame(ctx, frame, bbox, paths, opts = {}) {
    const st = this.style;
    const W = this.layout.width * this.ss;
    const H = this.layout.height * this.ss;
    const { vp, items } = paths;

    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.fillStyle = st.background;
    ctx.fillRect(0, 0, W, H);

    // 1) 填充：所有环都按外环填（契约里 rings 是拍平的，
    //    MultiPolygon 各部分都有自己的外环；当真洞处理会把苏联涂没）
    const alpha = st.regionAlpha;
    for (const it of items) {
      const color = it.feature.color
        || (st.palette.length ? st.palette[items.indexOf(it) % st.palette.length] : "#3a4048");
      it._fill = rgba(color, alpha);
      ctx.fillStyle = it._fill;
      for (const ring of it.rings) {
        if (ring.length < 3) continue;
        ctx.beginPath();
        ctx.moveTo(ring[0][0], ring[0][1]);
        for (let i = 1; i < ring.length; i++) ctx.lineTo(ring[i][0], ring[i][1]);
        ctx.closePath();
        ctx.fill();
      }
    }

    // 2) 描边
    const bw = st.borderWidth;
    if (bw > 0) {
      ctx.strokeStyle = st.borderColor;
      ctx.lineWidth = Math.max(1, bw * this.ss);
      ctx.lineJoin = "round";
      for (const it of items) {
        for (const ring of it.rings) {
          if (ring.length < 3) continue;
          ctx.beginPath();
          ctx.moveTo(ring[0][0], ring[0][1]);
          for (let i = 1; i < ring.length; i++) ctx.lineTo(ring[i][0], ring[i][1]);
          ctx.closePath();
          ctx.stroke();
        }
      }
    }

    // 3) 标注（面积闸门 + 碰撞避让 + 候选位）
    if (st.labelSize > 0) {
      this._drawLabels(ctx, items, vp);
    }

    // 4) 标题
    if (frame.title) {
      ctx.font = `bold ${st.titleSize * this.ss}px "Microsoft YaHei", sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const tx = W / 2, ty = H * this.layout.titleRatio * 0.42;
      ctx.lineWidth = 3 * this.ss * 2;
      ctx.strokeStyle = st.background;
      ctx.strokeText(frame.title, tx, ty);
      ctx.fillStyle = st.titleColor;
      ctx.fillText(frame.title, tx, ty);
    }
    if (frame.subtitle) {
      const size = st.subtitleSize * this.ss;
      ctx.font = `${size}px "Microsoft YaHei", sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const tx = W / 2, ty = H * this.layout.titleRatio * 0.78;
      ctx.lineWidth = 2 * this.ss * 2;
      ctx.strokeStyle = st.background;
      ctx.strokeText(frame.subtitle, tx, ty);
      ctx.fillStyle = st.subtitleColor;
      ctx.fillText(frame.subtitle, tx, ty);
    }

    // 5) 图例
    if (st.legend.enabled && opts.legendItems) {
      this._drawLegend(ctx, opts.legendItems, opts.legendTitle, W, H);
    }

    ctx.restore();
  }

  _drawLabels(ctx, items, vp) {
    const st = this.style;
    const size = st.labelSize * this.ss;
    const font = `${size}px "Microsoft YaHei", sans-serif`;
    ctx.font = font;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    // 必须累计**所有环**的面积：MultiPolygon 首环可能只是个小岛
    const areas = items.map((it) =>
      it.rings.reduce((s, r) => s + Math.abs(ringArea(r)), 0));
    const total = areas.reduce((a, b) => a + b, 0) || 1;
    const minRatio = st.labelMinAreaRatio;
    const placed = [];
    const placedNames = new Set();
    const order = items.map((_, i) => i).sort((a, b) => areas[b] - areas[a]);

    const vx0 = vp.x, vy0 = vp.y;
    const vx1 = vp.x + vp.width, vy1 = vp.y + vp.height;

    for (const i of order) {
      if (areas[i] / total < minRatio) continue;
      const it = items[i];
      const name = it.feature.name;
      // 只看「最大环」：苏联有 136 个环，遍历所有环找「质心在视口内的最大环」
      // 会让一个卡累利阿小环击败主环，把「苏联」标到瑞典头上。
      let big = null, bigA = -1;
      for (const r of it.rings) {
        const a = Math.abs(ringArea(r));
        if (a > bigA) { bigA = a; big = r; }
      }
      let cand = null;
      if (big) {
        const c = centroid(big);
        cand = (c[0] >= vx0 && c[0] <= vx1 && c[1] >= vy0 && c[1] <= vy1)
          ? c : bboxAnchor(it.rings, vp);
      }
      if (!cand) continue;
      // 多个实体可能映射到同一个中文名（1945 年有三个德国实体），同名只标一次
      if (placedNames.has(name)) continue;

      const tw = ctx.measureText(name).width;
      const th = size;
      const pad = 5 * this.ss;
      const dy = th * 0.95, dx = tw * 0.55;
      const tries = [[0, 0], [0, -dy], [0, dy], [-dx, 0], [dx, 0],
        [-dx, -dy], [dx, -dy], [-dx, dy], [dx, dy], [0, -2 * dy], [0, 2 * dy]];
      let done = false;
      for (const [ox, oy] of tries) {
        const x = cand[0] + ox, y = cand[1] + oy;
        if (x < vx0 || x > vx1 || y < vy0 || y > vy1) continue;
        const box = [x - tw / 2 - pad, y - th / 2 - pad,
          x + tw / 2 + pad, y + th / 2 + pad];
        if (collides(box, placed)) continue;
        placed.push(box);
        placedNames.add(name);
        ctx.lineWidth = st.labelHaloWidth * this.ss;
        ctx.strokeStyle = st.labelHalo;
        ctx.strokeText(name, x, y);
        ctx.fillStyle = st.labelColor;
        ctx.fillText(name, x, y);
        done = true;
        break;
      }
      if (!done && this.debugLabels) console.log(`[label] 丢弃 ${name}`);
    }
  }

  _drawLegend(ctx, items, title, W, H) {
    const st = this.style.legend;
    const size = st.size * this.ss;
    ctx.font = `${size}px "Microsoft YaHei", sans-serif`;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const pad = 14 * this.ss, sw = 22 * this.ss;
    const lineH = (st.size * 1.75) * this.ss;
    const headH = title ? lineH : 0;
    let textW = 0;
    for (const [, t] of items) textW = Math.max(textW, ctx.measureText(t).width);
    const boxW = pad * 2 + sw + 8 * this.ss + textW;
    const boxH = pad * 2 + lineH * items.length + headH;

    const pos = st.position || "bottom-left";
    const m = this.layout.margin * this.ss;
    const x0 = pos.includes("left") ? m : W - m - boxW;
    const y0 = pos.includes("top")
      ? H * this.layout.titleRatio * 1.05
      : H * (1 - this.layout.footerRatio) - boxH - m;

    ctx.fillStyle = rgba(this.style.background, 0.88);
    ctx.fillRect(x0, y0, boxW, boxH);
    ctx.strokeStyle = "#4a5568";
    ctx.lineWidth = Math.max(1, this.ss);
    ctx.strokeRect(x0, y0, boxW, boxH);

    if (title) {
      ctx.font = `${size * 1.05}px "Microsoft YaHei", sans-serif`;
      ctx.fillStyle = "#e2e8f0";
      ctx.fillText(title, x0 + pad, y0 + pad + lineH * 0.4);
    }
    ctx.font = `${size}px "Microsoft YaHei", sans-serif`;
    items.forEach(([name, color], i) => {
      const cy = y0 + pad + headH + lineH * i + lineH / 2;
      ctx.fillStyle = color;
      ctx.fillRect(x0 + pad, cy - sw / 3, sw, sw * 2 / 3);
      ctx.fillStyle = this.style.labelColor;
      ctx.fillText(name, x0 + pad + sw + 8 * this.ss, cy);
    });
  }

  /** 预先算出大事记面板的**固定**尺寸。
   *  必须固定：每帧按当帧文字长度算宽度的话，播放时的交叉淡入会让
   *  A 帧面板外的地名标注落在 B 帧面板内，混出「幽灵文字」。 */
  tickerBox(W, H, allRows, maxRows = 3) {
    const fs = Math.max(14, Math.round(H * 0.0195));
    const pad = Math.round(H * 0.016);
    const lh = Math.round(fs * 1.55);
    const probe = document.createElement("canvas").getContext("2d");
    probe.font = `${fs}px "Microsoft YaHei", sans-serif`;
    let w = 0;
    for (const rows of allRows) {
      for (const r of rows.slice(0, maxRows)) {
        w = Math.max(w, probe.measureText(`${r.date}  ${r.label}`).width);
      }
    }
    w = Math.min(w, W * 0.44);
    const bw = w + pad * 2, bh = lh * maxRows + pad * 2;
    const x1 = W - Math.round(W * 0.028), y1 = H - Math.round(H * 0.055);
    return { fs, pad, lh, w, x0: x1 - bw, y0: y1 - bh, x1, y1 };
  }

  /** 右下角「近期战局」面板（尺寸由 tickerBox 固定）。 */
  drawTicker(canvas, rows, box) {
    if (!rows.length || !box) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const { fs, pad, lh, x0, y0, x1, y1 } = box;
    ctx.save();
    ctx.font = `${fs}px "Microsoft YaHei", sans-serif`;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const texts = rows.map((r) => `${r.date}  ${r.label}`);

    ctx.fillStyle = "rgba(9,14,20,0.965)";
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    ctx.strokeStyle = "rgba(86,102,120,0.82)";
    ctx.lineWidth = 1;
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);

    ctx.font = `${Math.round(fs * 0.8)}px "Microsoft YaHei", sans-serif`;
    ctx.fillStyle = "rgb(150,165,180)";
    ctx.fillText("近 期 战 局", x0 + pad, y0 + pad * 0.6);
    ctx.font = `${fs}px "Microsoft YaHei", sans-serif`;
    ctx.fillStyle = "rgb(232,238,244)";
    texts.forEach((t, i) => {
      ctx.fillText(t, x0 + pad, y0 + pad + lh * (i + 0.5));
    });
    ctx.restore();
  }
}
