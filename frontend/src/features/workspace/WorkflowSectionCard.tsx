import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type WorkflowSectionCardProps = {
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  description?: ReactNode;
  title: ReactNode;
};

export function WorkflowSectionCard({
  actions,
  children,
  className,
  contentClassName,
  description,
  title,
}: WorkflowSectionCardProps) {
  return (
    <section className={cn("rounded border border-sidebar-border bg-sidebar", className)}>
      <header className="flex items-start justify-between gap-3 border-b border-sidebar-border px-4 py-3">
        <div className="min-w-0 space-y-1">
          <h3 className="text-sm font-medium text-foreground">{title}</h3>
          {description ? <div className="text-sm leading-6 text-muted-foreground">{description}</div> : null}
        </div>
        {actions ? <div className="shrink-0">{actions}</div> : null}
      </header>
      <div className={cn("px-4 py-4", contentClassName)}>{children}</div>
    </section>
  );
}
