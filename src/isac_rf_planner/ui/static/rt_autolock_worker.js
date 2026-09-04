import { evaluateOsmAutoLock } from "/rt_autolock.js";

self.onmessage = (event) => {
  const msg = event?.data || {};
  if (msg.type !== "evaluate_osm_autolock") return;
  try {
    const result = evaluateOsmAutoLock(msg.payload || {});
    self.postMessage({ ok: true, result });
  } catch (error) {
    self.postMessage({ ok: false, error: String(error) });
  }
};
