import { describe, expect, it } from "vitest";
import { DatabentoLiveDataSource, HistoricalFileDataSource, ReplayDataSource } from "@trading-assistant/trading-engine";
import type { MarketEvent } from "@trading-assistant/shared-types";

const event: MarketEvent = {
  version: 1, tsEventNs: "1", publisherId: 1, instrumentId: 42, sequence: 1,
  action: "A", side: "bid", priceFixed: "5000000000000", size: 1, flags: 128
};

describe("market data sources", () => {
  it("streams historical events through replay without changing order", async () => {
    const source = new ReplayDataSource(new HistoricalFileDataSource([event, { ...event, sequence: 2, tsEventNs: "2" }]));
    await source.connect();
    const values: MarketEvent[] = [];
    for await (const value of source.stream()) values.push(value);
    expect(values.map((value) => value.sequence)).toEqual([1, 2]);
    expect((await source.health()).mode).toBe("replay");
  });

  it("keeps live data disabled and exposes no order method", async () => {
    const source = new DatabentoLiveDataSource();
    await expect(source.connect()).rejects.toThrow("disabled");
    expect(await source.health()).toMatchObject({ state: "disabled", mode: "live" });
    expect("placeOrder" in source).toBe(false);
  });
});
