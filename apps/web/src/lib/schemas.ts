import { z } from "zod";

export const preTradeSchema = z.object({
  market: z.enum(["MES", "MNQ", "MGC", "GC"]),
  direction: z.enum(["LONG", "SHORT"]),
  date: z.string().min(1, "Datum ist erforderlich"),
  time: z.string().min(1, "Uhrzeit ist erforderlich"),
  session: z.enum(["RTH", "NYAM", "NYPM", "London", "Asia"]),
  biasTimeframe: z.string().min(1),
  setupTimeframe: z.string().min(1),
  entryTimeframe: z.string().min(1),
  entry: z.coerce.number().positive("Entry muss positiv sein"),
  stop: z.coerce.number().positive("Stop muss positiv sein"),
  target: z.coerce.number().positive("Target muss positiv sein"),
  contracts: z.coerce.number().int().positive("Mindestens ein Kontrakt"),
  setupName: z.string().min(2, "Setup-Name ist erforderlich")
});

export type PreTradeFormValues = z.infer<typeof preTradeSchema>;
