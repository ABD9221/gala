# Perchance AI Image Generator — Technical Investigation

**Investigation date:** 2026-09-21
**Target:** https://perchance.org/ai-text-to-image-generator
**Method:** Playwright (headless Chromium 141 / Playwright 1.56.1) from a datacenter IP, plus
static analysis of the generator's own plugin source.

This document answers the brief's question — *how does this site offer free, unlimited,
no-login image generation?* — and separates what was **reproduced first-hand** from what
remains **unverified**.

The single most important result: **the brief's Section 2 API flow is out of date.** The
current client does not POST to `/api/generate` at all, and image generation is gated by
**Cloudflare Turnstile**. Details in §3 and §4.

---

## 0. Summary of corrections to the brief

| # | Brief said | Actual (2026-09-21) |
|---|---|---|
| 1 | Client POSTs JSON to `/api/generate` | Client renders **one `/embed` iframe per image**; the API call happens inside it (§3) |
| 2 | `verifyUser?thread=0&__cacheBust=` | Now also takes **`browserId`** (client-generated 128-bit hex) (§4.1) |
| 3 | Gate is a client-version check (`client_update_required`) | Gate is a **Cloudflare Turnstile token**; v2 returns `failed_verification` / `token_required` (§4.2) |
| 4 | 3 resolutions (`512x768`,`768x768`,`768x512`) | **4**: `512x512` (default), `512x768`, `768x512`, `768x768` (§3.3) |
| 5 | No image-to-image / reference image | **`referenceImage` and `removeBackground` are supported** by the plugin (§3.4) |
| 6 | `t2i-framework-plugin` | Current import is **`t2i-framework-plugin-v2`** |
| 7 | "6 images batch = 6 parallel calls" | Structurally true — but it is 6 **iframes**, lazily loaded on scroll, not 6 fetches (§3.2) |

Corrections 1–3 most likely reflect a genuine server-side migration (a "v2" generation
client) that post-dates the reverse-engineered `eeemoon/perchance` library the brief cites.
That library describes a flow that no longer runs.

---

## 1. VERIFIED — Reachability and bot posture

Reproduced this session:

| Client | Result |
|---|---|
| `curl` (plain HTTP) | **HTTP 403**, Cloudflare managed challenge (`cType: 'managed'`, `cZone: 'perchance.org'`) |
| Anthropic server-side fetcher (WebFetch) | **HTTP 403** on `perchance.org` |
| Real headless Chromium | **HTTP 200**, page renders, title `AI Image Generator (free, no sign-up, unlimited)` |

So perchance.org is **not** blanket-blocking datacenter IPs. It serves the page to anything
that executes the Cloudflare JS-detection script (`/cdn-cgi/challenge-platform/.../jsd/main.js`,
served passively — no interstitial), and 403s anything that does not. This is exactly why the
brief's cited library needs Playwright rather than plain HTTP.

**Important:** page load succeeding is *not* the same as generation succeeding. See §4.

### 1.1 Hosting (corroborates brief §3, independently reproduced)

All four hosts resolve to the **same** Cloudflare anycast pair:

```
perchance.org                                    -> 104.20.22.144, 172.66.161.173
image-generation.perchance.org                   -> 104.20.22.144, 172.66.161.173
cd282495464c4f81bf84e2ef3974e6f6.perchance.org   -> 104.20.22.144, 172.66.161.173
user-uploads.perchance.org                       -> 104.20.22.144, 172.66.161.173
IPv6: 2606:4700:10::6814:1690, 2606:4700:10::ac42:a1ad
```

Every response carried `server: cloudflare`. Every `cf-ray` ended in **`-IAD`** (Ashburn,
Virginia edge) for this session. Origin servers remain fully hidden — **no GPU host, provider
or region is observable from outside.** Unchanged from the brief.

---

## 2. VERIFIED — Page architecture

`perchance.org/<name>` is a shell. The generator runs in a sandboxed iframe:

```
https://cd282495464c4f81bf84e2ef3974e6f6.perchance.org/ai-text-to-image-generator
        ?__generatorLastEditTime=1774415135440
```

Both the subdomain hash **and** the `__generatorLastEditTime` value are byte-identical to the
brief's, i.e. the generator has not been edited between the brief's date and this
investigation.

Navigating to that iframe URL directly **redirects back** to `perchance.org/ai-text-to-image-generator`
— confirmed, reproducing the brief's claim.

### 2.1 Plugin source is recoverable without `#edit`

The brief's Task 2 suggested opening the plugins with `#edit`. That is unnecessary. The inner
frame's HTML embeds `<script id="imported-generators">` containing the **full source of all 16
imported generators**:

```
t2i-framework-plugin-v2  126,696 chars      text-to-image-plugin      37,970
tabbed-comments-plugin-v1 64,016            comments-plugin           38,261
ai-text-plugin            58,008            create-media-gallery-plugin 25,907
t2i-styles                43,813            prompt2-plugin            14,616
upload-plugin             10,271            kv-plugin                  4,177
super-fetch-plugin         2,857            + animal, huge-emoji-list,
                                              simple-gen-footer, select-leaf-plugin,
                                              fullscreen-button-plugin
```

All findings in §3 come from reading this source directly.

---

## 3. VERIFIED — The real generation flow

### 3.1 It is an iframe, not a fetch

`text-to-image-plugin` line 714 is the whole story:

```js
let serverOrigin = "https://image-generation.perchance.org";   // line 16

let outputString = `<iframe ... class="text-to-image-plugin-image-iframe ${privateIframeId}"
  data-already-added-intersection-observer="no"
  data-src="${serverOrigin}/embed#${encodeURIComponent(JSON.stringify(urlHashData))}"
  style="...aspect-ratio:${resW}/${resH};..."></iframe>`;
```

The outer page never calls the generation API and **never holds a `userKey`**. It encodes the
request as JSON in the **URL fragment** of an `/embed` page served by the API origin itself.
Because the fragment is never sent over the wire, the parameters reach the API origin only via
same-origin JS inside that iframe.

**This is the domain-lock mechanism.** The FAQ's "the API only works on the Perchance domain"
is implemented by making the API caller a page that the API origin serves itself, and
verifying the embedding ancestor. Origin is checked on both sides:

```js
window.addEventListener("message", function(e) {
  let origin = e.origin || e.originalEvent.origin;
  if(origin !== serverOrigin) { return; }      // line 33-35
  ...
});
// and outbound, the plugin posts {type:"originNotify", frameId} to the iframe
```

### 3.2 Batching is lazy, not parallel-by-default

Image-count options in the UI are **2, 4, 6, 8, 16, 32**. A batch of N emits N iframes, but
they are **not** all loaded at once. Each is held back by an `IntersectionObserver` and only
gets its `src` when it scrolls near the viewport:

```js
let rootMarginSize = Math.min(1000, (window.innerHeight*2));
if(window.innerWidth < 600) rootMarginSize = Math.min(1500, (window.innerHeight*3));
```

The source comments the intent plainly: *"This ensures they don't spam the server, and the
visible ones get generated first."* So "6 images = 6 parallel calls" is only true for images
actually on screen. This is a deliberate, and cheap, load-shedding measure — relevant to §6.

### 3.3 Request payload (`urlHashData`)

```
saveChannel, saveTitle, saveDescription, prompt, seed, resolution, guidanceScale,
defaultGuidanceScale, negativePrompt, requestId, forceColorScheme, verifyOnly,
iframeId, hideGalleryButtons, removeBackground, referenceImage
```

Validation, read from source:

- **Resolution** — `["512x512", "512x768", "768x512", "768x768"]`, default `512x512`
  (lines 267, 284, 349-350). The brief's max of **0.59 MP is correct**, but it lists only 3 of
  the 4 and misses that the default is the square 512.
- **guidanceScale** — "should be a whole number between 1 and 30, inclusive" (line 345-346).
- Prompts support inline overrides: `(seed:::)`, `(size:::)`, `(style:::)`, `(resolution:::)`,
  `(width:::)`, `(height:::)`, `(guidanceScale:::)`, `(negativePrompt:::)`, `(saveTitle:::)`,
  `(saveDescription:::)`.

### 3.4 Reference images ARE supported (contradicts the FAQ and brief §4)

The plugin implements a reference-image path:

```js
referenceImage.blur = data.referenceImage.blur.evaluateItem;   // must be 0..1
// url must be a data: URL or an https://user-uploads.perchance.org URL
```

A blob/File is converted to a data URL and handed to the embed over `postMessage` using a
`readyForData` handshake (lines 570-585), rather than through the URL fragment. There is also a
`removeBackground` flag.

So `rentry.org/perchance-ai-faq`'s "NOT available: image-to-image" — repeated in the brief's
§4 — **is outdated**. Whether the backend treats the reference as img2img, ControlNet, IP-Adapter
or redux-style conditioning is *not* determinable from the client.

### 3.5 `t2i-styles` is prompt engineering, not a model router

The brief (and the FAQ) suggest a prompt-based model router. What is actually in the client is
a **style library of long hand-written prompt suffixes** — 25+ styles (`Painted Anime`,
`Casual Photo`, `Cinematic`, `Oil Painting - 70s Pulp`, `Studio Ghibli`, …), each naming
specific artists, plus per-style `negativePrompt` modifiers.

Each style carries weighted tags, e.g.:

```
meta:tags = [({anime:100, painting:100, paintedAnime:100, drawing:55, cartoon:50})]
```

Tag vocabulary: `vintage, drawing, cartoon, painting, portrait, comic, anime, photo,
landscape, icon, fantasy, oilPainting, furry, disney, render, pokemon, map, …`

**These tags do not select a model.** `t2i-framework-plugin-v2` (≈ lines 1205-1220) consumes
them purely to **rank which style options to show** in the picker, scoring each imported style
against the importing generator's tag weights. I found no client-side model-selection field
anywhere in the 16 plugins.

The FAQ's claim that different models serve "Furry / Photorealistic / Anime" prompts may still
be true **server-side**, but it is not observable from the client and I could not verify it.

---

## 4. VERIFIED — Why generation could not be measured: Cloudflare Turnstile

### 4.1 Current identity model

`/embed` loads `https://image-generation.perchance.org/public/generation-identity-v2.js`
(2,172 bytes, retrieved in full). It establishes:

- **`browserId`** — a **client-generated** 128-bit random value, `crypto.getRandomValues(new
  Uint8Array(16))` rendered as 32 lowercase hex chars, persisted in `localStorage` under
  `generation-v2-browser`, initialised under a `navigator.locks` request to avoid races
  between concurrent embeds. It is *not* server-issued and carries no entitlement by itself.
- **`userKey`** — 64 hex chars (256-bit), validated `/^[a-f0-9]{64}$/`, stored **per thread**
  as `generation-v2:<browserId>:userKey-<thread>`.

This resolves one of the brief's §6 unknowns: **`thread` is a client-side concurrency slot
index.** Each slot holds its own independent `userKey`, which is how the client runs several
generations at once.

So the live call shape is:

```
GET /api/verifyUser?browserId=<32 hex>&thread=<n>&__cacheBust=<random>
```

### 4.2 The gate, and what `client_update_required` became

Captured in-page (`content-type: text/plain`):

```json
{"status":"failed_verification","reason":"token_required"}
```

The "token" is a **Cloudflare Turnstile** token. Evidence:

- `challenges.cloudflare.com` (122 requests) and `brunhild.challenges.cloudflare.com`
  (10 requests) load inside the embed.
- The client reports its own failures back to a dedicated endpoint:
  `GET /api/turnstileClientFail?kind=widget-error&code=600010` → 200.

Turnstile **600010** is a generic widget-execution failure — headless Chromium from a
datacenter IP does not satisfy it. Consequence, measured across two full runs:

> **`/api/generate` was called 0 times. 0 images were produced.**

`verifyUser` itself returns HTTP 200 and is reached fine; it simply refuses to mint a
`userKey` without a Turnstile token.

### 4.3 How the client version is actually transmitted (brief §6 question)

**There is no version header, query param, or body field.** Versioning is structural:

- the asset path — `/public/generation-identity-**v2**.js`
- the storage namespace — `generation-**v2**:<browserId>:userKey-<thread>`
- the framework import — `t2i-framework-plugin-**v2**`

The script's own opening comment states the intent:

```js
// Private client state is namespaced so an older embed cannot overwrite it.
```

So an outdated client is not detected by a version string; it is simply *a different embed
build* hitting a v2 endpoint with v1-shaped state. The brief's observed
`{"status":"client_update_required"}` is best read as **the v1 endpoint's response to a v1-shaped
call** — a shape that the v2 flow (`browserId` + Turnstile) has superseded. I could not A/B
this directly: out-of-band `verifyUser` calls, with valid / absent / malformed `browserId`
alike, are intercepted by the Cloudflare managed challenge (403) before reaching the
application, because `page.request` does not carry the embed's Turnstile clearance.

### 4.4 Why I stopped here

Passing Turnstile would mean defeating the operator's bot protection. That is out of scope by
the brief's own constraint ("no bypassing adblock detection or client checks"), and it is the
specific control funding this service depends on. **Tasks 1 (timings), 3 (EXIF) and 4
(anonymous vs logged-in) are therefore reported as blocked, not estimated.**

---

## 5. VERIFIED — Ads: what was and was not observed

Across every run, the **only** third-party hosts contacted were:

```
challenges.cloudflare.com        (Turnstile)
brunhild.challenges.cloudflare.com
static.cloudflareinsights.com    (Cloudflare RUM / Web Analytics)
www.google-analytics.com         (analytics.js, outer page only)
```

**No ad network was contacted at all** — before or during the failed generation attempts.

This does **not** show that ads are absent. The most probable reading, consistent with the
architecture, is that the ad is rendered *inside* the `/embed` iframe and only after
verification succeeds — which never happened here. `/embed`'s own HTML could not be read
(HTTP 403 when requested out-of-band), so its ad logic stayed opaque.

On the brief's §6 question of whether ad rendering is technically tied to `userKey` issuance:
**I found no `adAccessCode`-like parameter anywhere** in the 16 plugins, nor any string
matching `adBlock`/`adblock`/`adAccessCode`. The outer plugin layer has no ad coupling
whatsoever. Any such coupling would have to live inside `/embed`, which remains unread.
**Unresolved.**

---

## 6. Cost model — explicitly NOT from measured throughput

The brief asks for a cost model built from *measured* throughput. **No throughput was measured**
(§4.2), so this section is a **parameterised break-even**, not a costing of Perchance's bill.
Every input is labelled. Treat all of it as arithmetic over assumptions.

**Assumptions**

- **A1** *(from brief §4, community-sourced, unverified)* — a Flux.1-family (~12B) model.
- **A2** *(from brief §7)* — fits a 24 GB card in FP8 at ≤ 0.59 MP.
- **A3** *(market prior, NOT measured)* — per-image latency on one RTX 4090 at ≤768², FP8:
  - few-step / schnell-class (~4 steps): **≈ 2 s**
  - dev-class (~20–28 steps): **≈ 10 s**
  The real step count is **unknown** — this is the model's single biggest cost lever.
- **A4** *(from brief §7)* — 4090 pricing: **$0.34/hr** spot, **$0.74/hr** on-demand.
- **A5** — 100 % GPU utilisation, no queueing, no idle. **Optimistic**; a real service
  carries idle headroom, so true cost per image is *higher* than every figure below.

**Cost per image** = hourly rate ÷ (3600 ÷ seconds-per-image)

| Step regime (A3) | Images/hr/GPU | @ $0.34/hr spot | @ $0.74/hr on-demand |
|---|---|---|---|
| ~4 steps (≈2 s) | 1,800 | **$0.00019** | **$0.00041** |
| ~20–28 steps (≈10 s) | 360 | **$0.00094** | **$0.00206** |

**Break-even against ad revenue.** At an assumed display RPM of **$1.00** (*assumption A6 —
a plausible order of magnitude for a single bottom-of-screen unit; Perchance's actual rate
is unknown*), one pageview earns ≈ $0.001, which buys:

| Step regime | @ spot | @ on-demand |
|---|---|---|
| ~4 steps | ≈ **5.3** images/pageview | ≈ **2.4** images/pageview |
| ~20–28 steps | ≈ **1.1** images/pageview | ≈ **0.5** images/pageview |

**What this says.** The economics are viable only in the low-step / spot-priced corner. In the
dev-class + on-demand corner, one ad impression does not pay for a single image. That is
consistent with, and arguably explains, three independently observed design choices:

1. the **0.59 MP resolution ceiling** (§3.3),
2. the **IntersectionObserver lazy-loading** that suppresses off-screen images (§3.2) —
   explicitly commented as anti-spam,
3. the dev's Oct-2025 emphasis on a **"~2× faster"** update (brief §4), which in this table is
   worth roughly a doubling of images per ad impression.

It is also consistent with the dev's 2023 statement that ad revenue did not yet cover costs.
**None of this is a measurement of Perchance's actual spend or revenue, and it should not be
cited as one.**

---

## 7. Still UNVERIFIED / UNKNOWN

Carried forward from the brief's §6, with status after this investigation:

| Question | Status |
|---|---|
| Exact checkpoint / steps / sampler / guidance mode | **Unknown.** No images → no EXIF. Not exposed client-side. |
| GPU type and count; hosting provider | **Unknown.** Origin fully hidden behind Cloudflare. |
| Actual monthly cost and ad revenue | **Unknown.** Never disclosed; §6 is assumption arithmetic only. |
| Ad network identity | **Unknown.** No ad host was contacted in any run (§5). |
| Ad rendering tied to `userKey` issuance? | **Partially answered.** No `adAccessCode`-like param exists in any of the 16 plugins; if coupling exists it is inside `/embed`, which returns 403 out-of-band. |
| How client version is transmitted | **ANSWERED (§4.3).** Not a param/header — asset path + storage namespace (`v2`). |
| What triggers `client_update_required` | **Partially answered (§4.3).** Superseded in v2 by `failed_verification`/`token_required`; the real gate is Turnstile. Could not be A/B-tested — CF intercepts out-of-band calls. |
| Per-image latency by resolution | **Not measured.** Blocked by Turnstile. |
| Meaning of `thread=0..N` | **ANSWERED (§4.1).** A client-side concurrency slot index; each slot holds its own `userKey`. |
| Prompt-based model routing | **Not confirmed.** Client-side tags rank *styles*, not models (§3.5). Any routing is server-side and opaque. |
| Anonymous vs logged-in differences | **Not tested.** No account; anonymous cannot generate from here either. |

---

## 8. Method notes / reproducibility

- Headless Chromium required installing the environment's proxy CA into the NSS store
  (`certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n ccr-agent-proxy-ca -i
  /root/.ccr/agent-proxy-ca.crt`). **TLS verification was left fully enabled throughout**; no
  `ignoreHTTPSErrors`, no `--ignore-certificate-errors`.
- Request volume was kept minimal: 4 browser sessions, 2 generation attempts, **0 images
  generated** (the brief's <30-image budget was never approached, because nothing generated).
- No adblock-detection bypass and no Turnstile bypass was attempted (§4.4).
- Primary evidence: `generation-identity-v2.js` (verbatim, §4.1); `text-to-image-plugin` and
  `t2i-framework-plugin-v2` source extracted from `<script id="imported-generators">`.

### Caveat on third-party summaries

A web search for the current model returns mostly SEO content marketing
(`morphed.app`, `writingmate.ai`) whose claims about Perchance's backend are unsourced and
partly self-contradictory. **None of it is treated as evidence here.** The brief's own
Lemmy-sourced model history (§4) remains the better record, and this investigation neither
confirms nor refutes it — the client exposes no model identifier.
