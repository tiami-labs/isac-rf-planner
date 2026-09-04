import { launchRayBatch as launchOsmRayBatch } from "/raytrace_3d_osm.js";

export function yawPitchDegTowardLocalVector(localDir) {
  const yawDeg = (Math.atan2(localDir.x, localDir.z) * 180.0) / Math.PI;
  const horiz = Math.hypot(localDir.x, localDir.z);
  const pitchDeg = (Math.atan2(localDir.y, Math.max(horiz, 1e-9)) * 180.0) / Math.PI;
  return { yawDeg, pitchDeg };
}

export function buildAutoLockSearch(baseYawDeg, basePitchDeg, hSpreadDeg, vSpreadDeg) {
  const coarseYaw = Math.max(3.0, hSpreadDeg * 0.35);
  const coarsePitch = Math.max(2.0, vSpreadDeg * 0.35);
  const fineYaw = Math.max(1.0, coarseYaw * 0.5);
  const finePitch = Math.max(0.75, coarsePitch * 0.5);
  const coarse = [
    [0, 0],
    [-coarseYaw, 0], [coarseYaw, 0],
    [0, -coarsePitch], [0, coarsePitch],
    [-coarseYaw, -coarsePitch], [-coarseYaw, coarsePitch],
    [coarseYaw, -coarsePitch], [coarseYaw, coarsePitch],
  ];
  const fineOffsets = [
    [-fineYaw, 0], [fineYaw, 0],
    [0, -finePitch], [0, finePitch],
    [-fineYaw, -finePitch], [-fineYaw, finePitch],
    [fineYaw, -finePitch], [fineYaw, finePitch],
  ];
  return {
    coarse: coarse.map(([dyaw, dpitch]) => ({
      yawDeg: baseYawDeg + dyaw,
      pitchDeg: basePitchDeg + dpitch,
      dyaw,
      dpitch,
    })),
    fineOffsets,
  };
}

export function scoreAutoLockCandidate(candidate, summary) {
  const hits = Number(summary?.hits || 0);
  const minBounce = Number.isFinite(Number(summary?.minBounce)) ? Number(summary.minBounce) : 99;
  return hits * 1000 - minBounce * 10 - Math.abs(candidate.dyaw) * 0.25 - Math.abs(candidate.dpitch) * 0.5;
}

export function chooseAutoLockAngles(baseAngles, best) {
  if (best && Number(best.summary?.hits || 0) > 0) {
    return { yawDeg: best.candidate.yawDeg, pitchDeg: best.candidate.pitchDeg, usedFallback: false };
  }
  return { yawDeg: baseAngles.yawDeg, pitchDeg: baseAngles.pitchDeg, usedFallback: true };
}

export function evaluateOsmAutoLock({
  txLocal,
  rxLocal,
  prisms,
  boxes,
  baseYawDeg,
  basePitchDeg,
  hSpreadDeg,
  vSpreadDeg,
  maxBounces,
  maxDistance,
  rxRadius,
  searchRayCount,
}) {
  const search = buildAutoLockSearch(baseYawDeg, basePitchDeg, hSpreadDeg, vSpreadDeg);
  let best = null;
  let checked = 0;

  const evaluate = (candidate) => {
    checked += 1;
    const batch = launchOsmRayBatch({
      tx: txLocal,
      rx: rxLocal,
      boxes,
      prisms,
      numRays: searchRayCount,
      yawDeg: candidate.yawDeg,
      pitchDeg: candidate.pitchDeg,
      hSpreadDeg,
      vSpreadDeg,
      maxBounces,
      maxDistance,
      rxRadius,
    });
    const summary = {
      hits: Number(batch?.hits || 0),
      minBounce: batch?.stats?.minBouncesAmongHits ?? null,
      maxHitBounce: batch?.stats?.maxBouncesAmongHits ?? null,
      bounceSurface: "osm_collision",
      collisionBoxes: Array.isArray(boxes) ? boxes.length : 0,
    };
    const score = scoreAutoLockCandidate(candidate, summary);
    if (!best || score > best.score) best = { candidate, summary, score };
  };

  for (const candidate of search.coarse) evaluate(candidate);
  for (const [dyaw, dpitch] of search.fineOffsets) {
    evaluate({
      yawDeg: best.candidate.yawDeg + dyaw,
      pitchDeg: best.candidate.pitchDeg + dpitch,
      dyaw: best.candidate.dyaw + dyaw,
      dpitch: best.candidate.dpitch + dpitch,
    });
  }

  const chosen = chooseAutoLockAngles({ yawDeg: baseYawDeg, pitchDeg: basePitchDeg }, best);
  return { best, chosen, checked };
}
