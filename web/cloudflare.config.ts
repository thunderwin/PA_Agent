import { defineConfig } from "cf/config";

export default defineConfig({
  worker: {
    "name": "web",
    "compatibilityDate": "2026-09-25",
    "observability": {
      "enabled": true
    }
  }
});
