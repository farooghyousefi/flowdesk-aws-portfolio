import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex h-9 items-center justify-center gap-2 rounded-md border text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-terminal-blue disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "border-terminal-blue/40 bg-terminal-blue text-white hover:bg-blue-500",
        secondary: "border-terminal-line bg-terminal-panel2 text-terminal-text hover:bg-terminal-line/70",
        ghost: "border-transparent bg-transparent text-terminal-soft hover:bg-terminal-panel2 hover:text-terminal-text",
        danger: "border-red-500/60 bg-red-500/15 text-red-200 hover:bg-red-500/25",
        success: "border-green-500/60 bg-green-500/15 text-green-100 hover:bg-green-500/25"
      },
      size: {
        default: "px-4",
        sm: "h-8 px-3 text-xs",
        icon: "h-9 w-9 p-0"
      }
    },
    defaultVariants: {
      variant: "default",
      size: "default"
    }
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  }
);
Button.displayName = "Button";
