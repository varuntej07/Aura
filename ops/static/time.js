(function initAuraOpsTime(root) {
  "use strict";

  const FUTURE_TOLERANCE_SECONDS = 300;

  function duration(seconds) {
    const value = Math.max(0, Math.floor(Math.abs(seconds)));
    if (value < 60) return value + "s";
    if (value < 3600) return Math.floor(value / 60) + "m";
    if (value < 86400) return Math.floor(value / 3600) + "h";
    return Math.floor(value / 86400) + "d";
  }

  function inspect(iso, nowMs) {
    if (!iso) return { label: "", state: "missing" };
    const timestampMs = Date.parse(iso);
    if (!Number.isFinite(timestampMs)) return { label: "invalid time", state: "invalid" };

    const deltaSeconds = ((nowMs ?? Date.now()) - timestampMs) / 1000;
    if (deltaSeconds < -FUTURE_TOLERANCE_SECONDS) {
      return { label: duration(deltaSeconds) + " ahead", state: "future" };
    }
    if (deltaSeconds < 5) return { label: "now", state: "current" };
    return { label: duration(deltaSeconds), state: "past" };
  }

  const api = {
    ago: (iso, nowMs) => inspect(iso, nowMs).label,
    inspect,
    isDisplayable: (iso, nowMs) => inspect(iso, nowMs).state !== "future",
    phrase: (iso, nowMs) => {
      const result = inspect(iso, nowMs);
      if (result.state === "current") return "just now";
      if (result.state === "past") return result.label + " ago";
      return result.label;
    },
  };

  root.AuraOpsTime = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
