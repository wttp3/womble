import { defineConfig, bindings, exports } from "cf/config";

export default defineConfig({
  worker: {
    name: "womble-alexa",
    entrypoint: "./src/index.js",
    compatibilityDate: "2026-09-30",
    workersDev: true,
    // Do not log spoken text: observability off.
    observability: { enabled: false },
    // Replaces a wrangler migration { tag: "v1", new_sqlite_classes: ["HubLink"] }.
    // The next `cf deploy` creates this SQLite-backed Durable Object class.
    exports: {
      HubLink: exports.durableObject({ storage: "sqlite" }),
    },
    env: {
      QUEUE: bindings.queue({ name: "womble-alexa" }),
      ALEXA_SKILL_ID: bindings.secret(),
      HUB_LINK_TOKEN: bindings.secret(),
      HUB_LINK: bindings.durableObject({
        worker: "womble-alexa",
        exportName: "HubLink",
      }),
    },
  },
});
