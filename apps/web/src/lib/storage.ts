"use client";

import { get, set } from "idb-keyval";
import { initialState } from "@/data/seed";
import type { JournalState } from "@/lib/types";

const STORE_KEY = "futures-journal-state-v1";

export async function loadJournalState(): Promise<JournalState> {
  const existing = await get<JournalState>(STORE_KEY);
  if (existing) return existing;
  await set(STORE_KEY, initialState);
  return initialState;
}

export async function saveJournalState(state: JournalState): Promise<void> {
  await set(STORE_KEY, state);
}

export async function restoreJournalState(state: JournalState): Promise<void> {
  await set(STORE_KEY, state);
}
