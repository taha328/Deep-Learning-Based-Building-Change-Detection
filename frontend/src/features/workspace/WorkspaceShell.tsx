import type { ComponentType, ReactNode } from "react";
import { ChevronLeft, User } from "lucide-react";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { ThemeSwitcher } from "@/components/ThemeSwitcher";
import { cn } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";

interface WorkspaceNavItem<T extends string> {
  id: T;
  icon: ComponentType<{ className?: string }>;
  label: string;
}

function RailButton({
  active,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className={cn(
        "flex h-11 w-11 items-center justify-center rounded text-muted-foreground transition-colors",
        active ? "text-primary" : "hover:text-foreground",
      )}
    >
      <Icon className="h-5 w-5" />
    </button>
  );
}

export function WorkspaceShell<T extends string>({
  brandLabel,
  activeTitle,
  navItems,
  activePanel,
  onActivePanelChange,
  isCollapsed,
  onToggleCollapse,
  footerContent,
  children,
}: {
  brandLabel: string;
  activeTitle: string;
  navItems: ReadonlyArray<WorkspaceNavItem<T>>;
  activePanel: T;
  onActivePanelChange: (panel: T) => void;
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  footerContent?: ReactNode;
  children: ReactNode;
}) {
  const { t } = useI18n();
  return (
    <aside
      className={cn(
        "flex h-auto min-h-0 shrink-0 border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-all duration-300 lg:h-full",
        isCollapsed ? "w-12" : "w-full lg:w-[444px]",
      )}
    >
      <div className="flex h-auto w-12 shrink-0 flex-col border-r border-sidebar-border bg-sidebar lg:h-full">
        <div className="h-10 shrink-0 border-b border-sidebar-border" />
        <nav className="flex flex-1 flex-col items-center gap-0.5 py-3">
          {navItems.map((item) => (
            <RailButton
              key={item.id}
              active={activePanel === item.id}
              icon={item.icon}
              label={item.label}
              onClick={() => {
                onActivePanelChange(item.id);
                if (isCollapsed) {
                  onToggleCollapse();
                }
              }}
            />
          ))}
        </nav>
        <div className="flex flex-col items-center gap-2 border-t border-sidebar-border px-2 py-3">
          {footerContent ? <div className="w-full px-1">{footerContent}</div> : null}
          <LanguageSwitcher />
          <ThemeSwitcher />
          <button
            type="button"
            title={t("panel.account")}
            aria-label={t("panel.account")}
            className="flex h-11 w-11 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground"
          >
            <User className="h-5 w-5" />
          </button>
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col bg-background lg:h-full">
        <div className="flex h-10 items-center justify-between border-b border-sidebar-border bg-sidebar px-4">
          <span className={cn("text-sm text-foreground", isCollapsed && "hidden")}>
            <span className="font-semibold">{brandLabel}</span>
          </span>
          <button
            type="button"
            onClick={onToggleCollapse}
            title={isCollapsed ? t("panel.expand") : t("panel.collapse")}
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground"
            aria-label={isCollapsed ? t("panel.expand") : t("panel.collapse")}
          >
            <ChevronLeft className={cn("h-4 w-4 transition-transform", isCollapsed && "rotate-180")} />
          </button>
        </div>

        <div className={cn("border-b border-sidebar-border bg-sidebar px-5 py-5", isCollapsed && "hidden")}>
          <h2 className="text-heading-lg font-semibold text-foreground">{activeTitle}</h2>
        </div>

        <div className={cn("min-h-0 flex-1 overflow-y-auto bg-background", isCollapsed && "hidden")}>{children}</div>
      </div>
    </aside>
  );
}
