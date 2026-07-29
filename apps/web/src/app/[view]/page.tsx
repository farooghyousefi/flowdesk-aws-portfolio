import { notFound } from "next/navigation";
import { MarketWorkspace } from "@/components/market/market-workspace";
import type { ViewName } from "@/components/market/types";

const routes: Record<string, ViewName> = {
  replay: "Replay",
  orderflow: "Orderflow",
  setups: "Setups",
  risk: "Risk",
  journal: "Journal",
  backtest: "Backtest",
  research: "Research Lab",
  "data-planner": "Data Planner",
  "data-health": "Data Health",
  settings: "Settings"
};

export default async function ViewPage({ params }: { params: Promise<{ view: string }> }): Promise<React.ReactElement> {
  const { view } = await params;
  const initialView = routes[view];
  if (!initialView) notFound();
  return <MarketWorkspace initialView={initialView} />;
}
