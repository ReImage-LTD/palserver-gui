import assert from "node:assert/strict";
import test from "node:test";
import { startThroughUpdateGate, startWithPalDefenderRepair } from "./routes.js";
import { acquireSaveCleanupLock } from "./save-operation-lock.js";
import type { DriverContext } from "./driver.js";
import type { InstanceRecord } from "./store.js";

const rec = { id: "route-start", backend: "native", settings: {} } as unknown as InstanceRecord;
const ctx: DriverContext = { instanceDir: "" };

test("start route runs the update gate before starting", async () => {
  const order: string[] = [];
  const supervisor = {
    applyUpdateBeforeStart: async () => {
      order.push("gate");
      return true;
    },
  };
  const result = await startThroughUpdateGate(supervisor, rec, ctx, async () => {
    order.push("start");
    return { started: true };
  });

  assert.deepEqual(order, ["gate", "start"]);
  assert.deepEqual(result, { started: true });
});

test("start route skips start when the gate sees a manual stop", async () => {
  let starts = 0;
  const supervisor = { applyUpdateBeforeStart: async () => false };
  const result = await startThroughUpdateGate(supervisor, rec, ctx, async () => {
    starts++;
    return { started: true };
  });

  assert.equal(result, null);
  assert.equal(starts, 0);
});

test("start route refuses to start while server save cleanup holds its lock", async () => {
  const release = acquireSaveCleanupLock(rec.id);
  try {
    await assert.rejects(
      startThroughUpdateGate({ applyUpdateBeforeStart: async () => true }, rec, ctx, async () => true),
      /Save cleanup is in progress/,
    );
  } finally {
    release();
  }
});

test("PalDefender repair does not report or refresh a failed second start", async () => {
  const repaired = { ...rec, name: "repaired" };
  let starts = 0;
  let refreshes = 0;
  const result = await startWithPalDefenderRepair(rec, {
    start: async () => {
      starts += 1;
      return starts === 1;
    },
    repair: async () => repaired,
    stop: async () => {},
    onStarted: async () => {
      refreshes += 1;
    },
  });

  assert.equal(starts, 2);
  assert.equal(result.started, false);
  assert.equal(result.rec, repaired);
  assert.equal(refreshes, 0);
});

test("manual start refreshes image evidence only after a successful final start", async () => {
  let refreshes = 0;
  const result = await startWithPalDefenderRepair(rec, {
    start: async () => true,
    repair: async () => rec,
    stop: async () => {},
    onStarted: async () => {
      refreshes += 1;
    },
  });

  assert.equal(result.started, true);
  assert.equal(refreshes, 1);
});
