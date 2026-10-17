// Alexa request verification (ASK "Host a custom skill as a web service").
import "reflect-metadata";
import * as x509 from "@peculiar/x509";
import { ROOTS } from "./roots.js";

x509.cryptoProvider.set(crypto);

const SAN = "echo-api.amazon.com";
const CACHE_TTL_MS = 6 * 3600 * 1000;
const certCache = new Map(); // url -> {certs, at}
const roots = ROOTS.map((p) => new x509.X509Certificate(p));
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
let rootSpki; // Set of SHA-256(SPKI) hex
async function rootSpkis() {
  if (!rootSpki) {
    rootSpki = new Set();
    for (const r of roots) rootSpki.add(hex(await crypto.subtle.digest("SHA-256", r.publicKey.rawData)));
  }
  return rootSpki;
}

export class VerifyError extends Error {}
const fail = (m) => { throw new VerifyError(m); };

// Rule 1: https, host s3.amazonaws.com (case-insensitive), path starts /echo.api/ (after normalising), port 443 if given.
export function checkCertUrl(raw) {
  if (!raw) fail("no cert url");
  let u;
  try { u = new URL(raw); } catch { fail("bad cert url"); }
  if (u.protocol !== "https:") fail("cert url not https");
  if (u.hostname.toLowerCase() !== "s3.amazonaws.com") fail("cert url host");
  if (u.port && u.port !== "443") fail("cert url port");
  if (!u.pathname.startsWith("/echo.api/")) fail("cert url path"); // URL() already resolves ../
  return u.toString();
}

function parsePemChain(pem) {
  const blocks = pem.match(/-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g) || [];
  if (!blocks.length) fail("empty chain");
  return blocks.map((b) => new x509.X509Certificate(b));
}

// Rule 2: chain valid now, leaf SAN = echo-api.amazon.com, chains to a trusted root.
export async function verifyChain(certs, now = new Date()) {
  const leaf = certs[0];
  const inDate = (c) => now >= c.notBefore && now <= c.notAfter;
  const san = leaf.getExtension(x509.SubjectAlternativeNameExtension);
  const names = san ? san.names.items.filter((n) => n.type === "dns").map((n) => n.value.toLowerCase()) : [];
  if (!names.includes(SAN)) fail("leaf SAN");
  const trusted = await rootSpkis();
  for (let i = 0; i < certs.length; i++) {
    const c = certs[i];
    if (!inDate(c)) fail(`cert ${i} not valid now`);
    if (i > 0) {
      const bc = c.getExtension(x509.BasicConstraintsExtension);
      if (!bc || !bc.ca) fail(`cert ${i} not a CA`);
    }
    const spki = hex(await crypto.subtle.digest("SHA-256", c.publicKey.rawData));
    if (i > 0 && trusted.has(spki)) return leaf; // reached a pinned root (verified up to here)
    const issuer = certs[i + 1];
    if (issuer) {
      if (c.issuer !== issuer.subject) fail(`cert ${i} issuer mismatch`);
      if (!(await c.verify({ publicKey: issuer.publicKey, signatureOnly: true }))) fail(`cert ${i} bad signature`);
    } else {
      const root = roots.find((r) => r.subject === c.issuer);
      if (!root) fail("untrusted root");
      if (!(await c.verify({ publicKey: root.publicKey, signatureOnly: true }))) fail("bad root signature");
      return leaf;
    }
  }
  fail("chain did not terminate");
}

async function getChain(url, fetchImpl) {
  const hit = certCache.get(url);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.certs;
  const r = await fetchImpl(url, { cf: { cacheTtl: 3600, cacheEverything: true } });
  if (!r.ok) fail("cert fetch " + r.status);
  const certs = parsePemChain(await r.text());
  certCache.set(url, { certs, at: Date.now() });
  return certs;
}

// Rule 3: Signature-256 = base64 RSA-SHA256 over the raw body, with the leaf key.
export async function verifyAlexaRequest(headers, rawBody, fetchImpl = fetch) {
  const url = checkCertUrl(headers.get("SignatureCertChainUrl"));
  const sigB64 = headers.get("Signature-256");
  if (!sigB64) fail("no Signature-256");
  let sig;
  try { sig = Uint8Array.from(atob(sigB64), (ch) => ch.charCodeAt(0)); } catch { fail("bad signature b64"); }
  const leaf = await verifyChain(await getChain(url, fetchImpl));
  const key = await crypto.subtle.importKey("spki", leaf.publicKey.rawData,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, sig, new TextEncoder().encode(rawBody));
  if (!ok) fail("body signature");
}
