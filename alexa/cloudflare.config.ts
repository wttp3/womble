import { defineConfig, bindings } from "cf/config";

export default defineConfig({
  worker: {
    name: "womble-alexa",
    entrypoint: "./src/index.js",
    compatibilityDate: "2026-09-30",
    workersDev: true,
    // Do not log spoken text: observability off.
    observability: { enabled: false },
    env: {
      QUEUE: bindings.queue({ name: "womble-alexa" }),
      ALEXA_SKILL_ID: bindings.secret(),
    },
  },
});
