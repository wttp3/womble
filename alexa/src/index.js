import { handle } from "./app.js";
import { verifyAlexaRequest } from "./verify.js";

export default {
  async fetch(request, env) {
    return handle(request, env, (h, raw) => verifyAlexaRequest(h, raw));
  },
};
