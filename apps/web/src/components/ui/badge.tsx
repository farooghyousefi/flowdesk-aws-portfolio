import { cn } from "@/lib/utils";

interface BadgeProps {
  children: React.ReactNode;
  tone?: "green" | "red" | "amber" | "blue" | "neutral";
  className?: string;
}

const toneClass: Record<NonNullable<BadgeProps["tone"]>, string> = {
  green: "border-green-500/45 bg-green-500/12 text-green-300",
  red: "border-red-500/45 bg-red-500/12 text-red-300",
  amber: "border-amber-500/45 bg-amber-500/12 text-amber-300",
  blue: "border-blue-500/45 bg-blue-500/12 text-blue-300",
  neutral: "border-terminal-line bg-terminal-panel2 text-terminal-soft"
};

export function Badge({ children, tone = "neutral", className }: BadgeProps): React.ReactElement {
  return (
    <span className={cn("inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-semibold", toneClass[tone], className)}>
      {children}
    </span>
  );
}
