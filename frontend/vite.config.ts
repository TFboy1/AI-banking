import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: { rollupOptions: { input: { banking: "index.html", securities: "securities.html", food: "food.html", travel: "travel.html", admin: "admin.html" } } },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
