import { handle } from "./app.js";
import { verifyAlexaRequest } from "./verify.js";
import { HubLink, routeHub } from "./hub_link.js";

export { HubLink };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/hub") return routeHub(request, env);
    return handle(request, env, (h, raw) => verifyAlexaRequest(h, raw));
  },
};
