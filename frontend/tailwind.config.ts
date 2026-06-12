import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Slate accent ramp (matches the .redesign design system). Remapping
        // `brand` re-skins every brand-* usage app-wide to the redesign accent.
        // 50/100/200/500/600/700 are the prototype's exact tokens; 300/400/800/900
        // are interpolated to keep all Tailwind shades valid.
        brand: {
          50: "#EEF2F8",
          100: "#DBE3F0",
          200: "#C0CDE2",
          300: "#9DB0CE",
          400: "#7389AE",
          500: "#4A689B",
          600: "#3C5681",
          700: "#314869",
          800: "#283A55",
          900: "#1F2D42",
        },
      },
      fontFamily: {
        sans: [
          '"Inter"',
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          "Roboto",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
      },
    },
  },
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  plugins: [require("@tailwindcss/forms")],
} satisfies Config;
