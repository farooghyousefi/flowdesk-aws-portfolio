import type { DataSourceHealth, MarketDataSource, MarketEvent } from "@trading-assistant/shared-types";

export class HistoricalFileDataSource implements MarketDataSource {
  private connected = false;

  constructor(private readonly events: readonly MarketEvent[]) {}

  async connect(): Promise<void> { this.connected = true; }
  async disconnect(): Promise<void> { this.connected = false; }

  async *stream(): AsyncIterable<MarketEvent> {
    if (!this.connected) throw new Error("Historical data source is not connected.");
    for (const event of this.events) yield event;
  }

  async health(): Promise<DataSourceHealth> {
    return { state: this.connected ? "connected" : "disconnected", mode: "historical", message: `${this.events.length} immutable events available.` };
  }
}

export class ReplayDataSource implements MarketDataSource {
  private connected = false;

  constructor(private readonly source: MarketDataSource) {}

  async connect(): Promise<void> { await this.source.connect(); this.connected = true; }
  async disconnect(): Promise<void> { this.connected = false; await this.source.disconnect(); }

  async *stream(): AsyncIterable<MarketEvent> {
    if (!this.connected) throw new Error("Replay data source is not connected.");
    for await (const event of this.source.stream()) yield event;
  }

  async health(): Promise<DataSourceHealth> {
    const upstream = await this.source.health();
    return { ...upstream, mode: "replay", message: this.connected ? "Replay source connected." : "Replay source disconnected." };
  }
}

export class DatabentoLiveDataSource implements MarketDataSource {
  async connect(): Promise<void> {
    throw new Error("Databento live is disabled. Replay mode remains active.");
  }
  async disconnect(): Promise<void> {}
  async *stream(): AsyncIterable<MarketEvent> {
    throw new Error("Databento live is disabled.");
  }
  async health(): Promise<DataSourceHealth> {
    return { state: "disabled", mode: "live", message: "Live data unavailable – Replay mode active" };
  }
}
