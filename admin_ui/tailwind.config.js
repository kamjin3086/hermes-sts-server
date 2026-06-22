export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["IBM Plex Sans", "Inter", "ui-sans-serif", "system-ui"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular"],
      },
      colors: {
        deck: "#101412",
        panel: "#181d19",
        line: "rgba(232,236,224,.12)",
        mint: "#5be3d1",
        amber: "#d7a84a",
        ember: "#e56d4f",
        paper: "#ebe5d2",
      },
    },
  },
  plugins: [],
};
