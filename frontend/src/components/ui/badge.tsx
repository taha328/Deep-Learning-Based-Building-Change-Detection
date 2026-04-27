import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

export function Badge({ className, ...props }: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "label-xs-accent inline-flex items-center rounded-md bg-secondary px-2 py-1 text-secondary-foreground",
        className,
      )}
      {...props}
    />
  );
}
