const test = require("node:test");
const assert = require("node:assert/strict");
const { ago, inspect, isDisplayable, phrase } = require("../static/time.js");

const NOW = Date.parse("2026-07-29T12:00:00Z");

test("formats ordinary past timestamps", () => {
  assert.equal(ago("2026-07-29T11:58:10Z", NOW), "1m");
});

test("labels materially future timestamps without a negative age", () => {
  assert.deepEqual(
    inspect("2026-08-06T01:00:00Z", NOW),
    { label: "7d ahead", state: "future" },
  );
});

test("excludes materially future timestamps from dashboard feeds", () => {
  assert.equal(isDisplayable("2026-08-06T01:00:00Z", NOW), false);
  assert.equal(isDisplayable("2026-07-29T11:58:10Z", NOW), true);
});

test("treats small clock differences as current", () => {
  assert.deepEqual(
    inspect("2026-07-29T12:02:00Z", NOW),
    { label: "now", state: "current" },
  );
});

test("builds natural relative-time phrases", () => {
  assert.equal(phrase("2026-07-29T11:58:10Z", NOW), "1m ago");
  assert.equal(phrase("2026-07-29T12:00:00Z", NOW), "just now");
  assert.equal(phrase("2026-08-06T01:00:00Z", NOW), "7d ahead");
});

test("labels malformed timestamps explicitly", () => {
  assert.deepEqual(inspect("not-a-date", NOW), {
    label: "invalid time",
    state: "invalid",
  });
});
