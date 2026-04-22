import type { Config } from "tailwindcss";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "oklch(var(--background) / <alpha-value>)",
        foreground: "oklch(var(--foreground) / <alpha-value>)",
        primary: {
          DEFAULT: "oklch(var(--primary) / <alpha-value>)",
          foreground: "oklch(var(--primary-foreground) / <alpha-value>)",
        },
        destructive: {
          DEFAULT: "oklch(var(--destructive) / <alpha-value>)",
          foreground: "oklch(var(--destructive-foreground) / <alpha-value>)",
        },
        warning: {
          DEFAULT: "oklch(var(--warning) / <alpha-value>)",
          foreground: "oklch(var(--warning-foreground) / <alpha-value>)",
        },
        secondary: {
          DEFAULT: "oklch(var(--secondary) / <alpha-value>)",
          foreground: "oklch(var(--secondary-foreground) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "oklch(var(--accent) / <alpha-value>)",
          foreground: "oklch(var(--accent-foreground) / <alpha-value>)",
        },
        border: "oklch(var(--border) / <alpha-value>)",
        muted: {
          DEFAULT: "oklch(var(--muted) / <alpha-value>)",
          foreground: "oklch(var(--muted-foreground) / <alpha-value>)",
        },
        card: "oklch(var(--card) / <alpha-value>)",
        ring: "oklch(var(--ring) / <alpha-value>)",
        surface: {
          DEFAULT: "oklch(var(--surface) / <alpha-value>)",
          foreground: "oklch(var(--surface-foreground) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["\"Public Sans\"", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["\"IBM Plex Mono\"", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        "caption": ["0.75rem", { lineHeight: "1rem", letterSpacing: "0.01em" }],
        "label": ["0.875rem", { lineHeight: "1.25rem", letterSpacing: "0.005em" }],
        "body": ["1rem", { lineHeight: "1.5rem" }],
        "body-lg": ["1.125rem", { lineHeight: "1.75rem" }],
        "heading-sm": ["1.125rem", { lineHeight: "1.375rem", letterSpacing: "-0.01em" }],
        "heading": ["1.5rem", { lineHeight: "1.875rem", letterSpacing: "-0.015em" }],
        "heading-lg": ["2rem", { lineHeight: "2.375rem", letterSpacing: "-0.02em" }],
      },
      boxShadow: {
        panel: "0 10px 30px rgba(16, 24, 40, 0.08)",
      },
      backgroundImage: {
        canvas:
          "linear-gradient(180deg, oklch(var(--background)) 0%, color-mix(in oklch, oklch(var(--background)) 86%, oklch(var(--primary)) 14%) 100%)",
      },
    },
  },
  plugins: [],
} satisfies Config;
