import { handle } from "./app.js";
import { verifyAlexaRequest } from "./verify.js";
import { HubLink, routeHub } from "./hub_link.js";
import { routeRequests } from "./reqlog.js";

export { HubLink };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/hub") return routeHub(request, env);
    if (url.pathname === "/admin/requests") return routeRequests(request, env);
    const waitUntil = ctx?.waitUntil ? (p) => ctx.waitUntil(p) : undefined;
    return handle(request, env, (h, raw) => verifyAlexaRequest(h, raw), { waitUntil });
  },
};
