import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/by_framework_chat_ui/static",
    emptyOutDir: false,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "[name].js",
        assetFileNames: "styles.css",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
