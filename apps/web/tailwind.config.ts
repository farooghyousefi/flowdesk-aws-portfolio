import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/lib/**/*.{ts,tsx}"
  ],
  theme: {
    extend: {
      colors: {
        terminal: {
          ink: "#061015",
          panel: "#0b171d",
          panel2: "#0f2028",
          line: "#24343d",
          soft: "#9aa9b1",
          text: "#e5edf0",
          green: "#5bd66f",
          red: "#ff4d57",
          amber: "#f4b43e",
          blue: "#2d8cff"
        }
      },
      fontFamily: {
        sans: ["var(--font-inter)", "Inter", "ui-sans-serif", "system-ui"],
        mono: ["var(--font-jetbrains)", "JetBrains Mono", "ui-monospace", "SFMono-Regular"]
      },
      boxShadow: {
        terminal: "0 18px 60px rgba(0, 0, 0, 0.35)"
      }
    }
  },
  plugins: []
};

export default config;
