/**
 * Client-side 3D specular ray tracing against OSM footprints extruded as axis-aligned
 * boxes in a local ENU-style frame (x=east, y=up, z=north), matching tests/raytrace_3d_demo.html.
 * Camera / Cesium view has no effect on physics — only this module's numeric scene does.
 */

const R_EARTH = 6371000.0;

const V = {
  add: (a, b) => ({ x: a.x + b.x, y: a.y + b.y, z: a.z + b.z }),
  sub: (a, b) => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z }),
  mul: (a, s) => ({ x: a.x * s, y: a.y * s, z: a.z * s }),
  dot: (a, b) => a.x * b.x + a.y * b.y + a.z * b.z,
  len: (a) => Math.hypot(a.x, a.y, a.z),
  norm: (a) => {
    const L = Math.hypot(a.x, a.y, a.z) || 1;
    return { x: a.x / L, y: a.y / L, z: a.z / L };
  },
  cross: (a, b) => ({
    x: a.y * b.z - a.z * b.y,
    y: a.z * b.x - a.x * b.z,
    z: a.x * b.y - a.y * b.x,
  }),
  reflect: (d, n) => V.sub(d, V.mul(n, 2 * V.dot(d, n))),
};

function clamp(x, a, b) {
  return Math.max(a, Math.min(b, x));
}

function rad(deg) {
  return (deg * Math.PI) / 180;
}

export function enuHorizontalFromOrigin(originLatDeg, originLonDeg, latDeg, lonDeg) {
  const lat0 = (originLatDeg * Math.PI) / 180;
  const dLat = ((latDeg - originLatDeg) * Math.PI) / 180;
  const dLon = ((lonDeg - originLonDeg) * Math.PI) / 180;
  return {
    east_m: dLon * R_EARTH * Math.cos(lat0),
    north_m: dLat * R_EARTH,
  };
}

export function normalizeBuildingRing(geometry) {
  if (!Array.isArray(geometry)) return [];
  const ring = [];
  for (const p of geometry) {
    const lat = Number(p?.lat);
    const lon = Number(p?.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const prev = ring[ring.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-12 && Math.abs(prev.lon - lon) < 1e-12) continue;
    ring.push({ lat, lon });
  }
  if (ring.length >= 2) {
    const first = ring[0];
    const last = ring[ring.length - 1];
    if (Math.abs(first.lat - last.lat) < 1e-12 && Math.abs(first.lon - last.lon) < 1e-12) {
      ring.pop();
    }
  }
  return ring.length >= 3 ? ring : [];
}

/**
 * @param {object[]} buildings — API /api/osm/buildings-near-point items
 * @param {number} originLat
 * @param {number} originLon
 * @param {{ defaultHeightM: number, minHeightM: number }} opts
 * @returns {{ min: {x,y,z}, max:{x,y,z} }[]}
 */
export function buildOsmBuildingAABBs(buildings, originLat, originLon, opts) {
  const defaultHeightM = Number(opts?.defaultHeightM) > 0 ? Number(opts.defaultHeightM) : 10.0;
  const minHeightM = Number(opts?.minHeightM) > 0 ? Number(opts.minHeightM) : 3.0;
  const boxes = [];
  if (!Array.isArray(buildings)) return boxes;

  for (const b of buildings) {
    const ring = normalizeBuildingRing(b?.geometry || []);
    if (ring.length < 3) continue;

    let minX = Infinity;
    let maxX = -Infinity;
    let minZ = Infinity;
    let maxZ = -Infinity;
    for (const p of ring) {
      const { east_m, north_m } = enuHorizontalFromOrigin(originLat, originLon, p.lat, p.lon);
      minX = Math.min(minX, east_m);
      maxX = Math.max(maxX, east_m);
      minZ = Math.min(minZ, north_m);
      maxZ = Math.max(maxZ, north_m);
    }

    let h = Number(b.height_m);
    if (!Number.isFinite(h) || h < minHeightM) h = defaultHeightM;
    const sampled = Number(b.sampled_height_m);
    if (Number.isFinite(sampled) && sampled > h) h = sampled;

    boxes.push({
      min: { x: minX, y: 0, z: minZ },
      max: { x: maxX, y: h, z: maxZ },
    });
  }
  return boxes;
}

export function dirFromYawPitch(yawDeg, pitchDeg) {
  const yaw = rad(yawDeg);
  const pitch = rad(pitchDeg);
  const cp = Math.cos(pitch);
  return V.norm({ x: cp * Math.sin(yaw), y: Math.sin(pitch), z: cp * Math.cos(yaw) });
}

export function basisFromForward(forward) {
  const upGuess = Math.abs(forward.y) > 0.98 ? { x: 1, y: 0, z: 0 } : { x: 0, y: 1, z: 0 };
  const right = V.norm(V.cross(forward, upGuess));
  const up = V.norm(V.cross(right, forward));
  return { right, up };
}

export function sampleBeamDirections(n, forward, hSpreadDeg, vSpreadDeg) {
  const dirs = [];
  const { right, up } = basisFromForward(forward);
  const h = rad(hSpreadDeg / 2);
  const v = rad(vSpreadDeg / 2);
  if (n === 1) return [forward];
  for (let i = 0; i < n; i++) {
    const u = (i + 0.5) / n;
    const a = (Math.sqrt(5) - 1) / 2;
    const j = (i * a) % 1;
    const yawOff = (j * 2 - 1) * h;
    const pitchOff = (u * 2 - 1) * v;
    const d = V.add(forward, V.add(V.mul(right, Math.tan(yawOff)), V.mul(up, Math.tan(pitchOff))));
    dirs.push(V.norm(d));
  }
  return dirs;
}

export function raySphereFirst(origin, dir, center, radius) {
  const oc = V.sub(origin, center);
  const b = V.dot(oc, dir);
  const c = V.dot(oc, oc) - radius * radius;
  const disc = b * b - c;
  if (disc < 0) return null;
  const s = Math.sqrt(disc);
  const t1 = -b - s;
  const t2 = -b + s;
  if (t1 > 1e-6) return t1;
  if (t2 > 1e-6) return t2;
  return null;
}

export function rayAabbFirst(origin, dir, box) {
  let tmin = -Infinity;
  let tmax = Infinity;
  let hitAxis = null;
  let hitSign = 0;
  for (const axis of ["x", "y", "z"]) {
    if (Math.abs(dir[axis]) < 1e-9) {
      if (origin[axis] < box.min[axis] || origin[axis] > box.max[axis]) return null;
    } else {
      let t1 = (box.min[axis] - origin[axis]) / dir[axis];
      let t2 = (box.max[axis] - origin[axis]) / dir[axis];
      let enterSign = -1;
      if (t1 > t2) {
        const s = t1;
        t1 = t2;
        t2 = s;
        enterSign = 1;
      }
      if (t1 > tmin) {
        tmin = t1;
        hitAxis = axis;
        hitSign = enterSign;
      }
      tmax = Math.min(tmax, t2);
      if (tmin > tmax) return null;
    }
  }
  if (tmax < 1e-6) return null;
  const t = tmin > 1e-6 ? tmin : tmax;
  const p = V.add(origin, V.mul(dir, t));
  const n = { x: 0, y: 0, z: 0 };
  n[hitAxis] = hitSign;
  const eps = 1e-5;
  let cornerHits = 0;
  if (Math.abs(p.x - box.min.x) < eps || Math.abs(p.x - box.max.x) < eps) cornerHits++;
  if (Math.abs(p.y - box.min.y) < eps || Math.abs(p.y - box.max.y) < eps) cornerHits++;
  if (Math.abs(p.z - box.min.z) < eps || Math.abs(p.z - box.max.z) < eps) cornerHits++;
  return { t, point: p, normal: n, box, isEdgeOrCorner: cornerHits >= 2 };
}

export function traceOneRay(origin, dir, boxes, maxBounces, maxDistance, rxCenter, rxRadius) {
  const pts = [origin];
  const segments = [];
  let pos = origin;
  let d = V.norm(dir);
  let traveled = 0;
  let bounces = 0;
  let hitRx = false;

  for (let iter = 0; iter <= maxBounces; iter++) {
    let nearestBoxHit = null;
    for (const box of boxes) {
      const hit = rayAabbFirst(pos, d, box);
      if (!hit) continue;
      if (hit.t <= 1e-5) continue;
      if (!nearestBoxHit || hit.t < nearestBoxHit.t) nearestBoxHit = hit;
    }

    const tRx = raySphereFirst(pos, d, rxCenter, rxRadius);
    const boundaryT = maxDistance - traveled;

    if (tRx !== null && tRx <= boundaryT && (!nearestBoxHit || tRx < nearestBoxHit.t)) {
      const p = V.add(pos, V.mul(d, tRx));
      pts.push(p);
      segments.push({ a: pos, b: p, kind: "hit" });
      hitRx = true;
      break;
    }

    if (!nearestBoxHit || nearestBoxHit.t > boundaryT) {
      const p = V.add(pos, V.mul(d, boundaryT));
      pts.push(p);
      segments.push({ a: pos, b: p, kind: "miss" });
      break;
    }

    if (nearestBoxHit.isEdgeOrCorner) {
      const p = nearestBoxHit.point;
      pts.push(p);
      segments.push({ a: pos, b: p, kind: "corner-reject" });
      break;
    }

    const hit = nearestBoxHit;
    traveled += hit.t;
    pts.push(hit.point);
    segments.push({ a: pos, b: hit.point, kind: "bounce" });
    if (iter === maxBounces) break;
    d = V.norm(V.reflect(d, hit.normal));
    pos = V.add(hit.point, V.mul(d, 1e-4));
    bounces++;
  }

  return { points: pts, segments, bounces, hitRx };
}

/**
 * @returns {{ rays: ReturnType<typeof traceOneRay>[], hits: number, stats: object }}
 */
export function launchRayBatch({
  tx,
  rx,
  boxes,
  numRays,
  yawDeg,
  pitchDeg,
  hSpreadDeg,
  vSpreadDeg,
  maxBounces,
  maxDistance,
  rxRadius,
}) {
  const forward = dirFromYawPitch(yawDeg, pitchDeg);
  const dirs = sampleBeamDirections(numRays, forward, hSpreadDeg, vSpreadDeg);
  const rays = dirs.map((d) => traceOneRay(tx, d, boxes, maxBounces, maxDistance, rx, rxRadius));
  let hits = 0;
  let best = Infinity;
  let maxHitBounces = -1;
  for (const r of rays) {
    if (r.hitRx) {
      hits++;
      best = Math.min(best, r.bounces);
      maxHitBounces = Math.max(maxHitBounces, r.bounces);
    }
  }
  return {
    rays,
    hits,
    stats: {
      rayCount: rays.length,
      hitCount: hits,
      minBouncesAmongHits: hits ? best : null,
      maxBouncesAmongHits: hits ? maxHitBounces : null,
    },
  };
}

/** Yaw (deg) to point boresight at ground point (x,z) from TX; pitch unchanged (demo rule). */
export function steerYawDegTowardGround(txEnu, groundEastM, groundNorthM) {
  const d = V.sub({ x: groundEastM, y: txEnu.y, z: groundNorthM }, txEnu);
  return (Math.atan2(d.x, d.z) * 180) / Math.PI;
}

export { V, clamp, rad };
