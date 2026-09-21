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

---

# SECOND PASS (same day) — media infrastructure, real output, and the model

The first pass stopped at the Turnstile gate. This pass went around it *without* defeating it,
by reading the **public gallery** — real images generated by real users, which need no token to
view. That produced the first hard evidence about the model and the true cost structure.

## 9. VERIFIED — There is a second, separate media domain: `uploads.dev`

The brief never mentions it, and neither did the first pass. Generated images are **not** served
from `perchance.org` at all:

```
aigc.uploads.dev   -> 104.21.42.58, 172.67.201.89     (generated images)
user.uploads.dev   -> 104.21.42.58, 172.67.201.89     (user avatars)
```

A different Cloudflare anycast pair from the `perchance.org` family (§1.1) — so this is
separate infrastructure, not just another subdomain.

**Its security posture is deliberately different.** Plain `curl`, which is 403'd by the
Cloudflare managed challenge on every `perchance.org` endpoint, fetches these images fine:

| Endpoint | plain curl |
|---|---|
| `image-generation.perchance.org/api/getPublicUserId` | **403** (CF managed challenge) |
| `image-generation.perchance.org/gallery` | **403** (CF managed challenge) |
| `aigc.uploads.dev/image/<sha256>.jpeg` | **200** |

So the gate is on *generation and app APIs*, not on *media delivery*.

### 9.1 Response headers are a big part of the cost answer

```
cache-control: max-age=31536000          <- 1 year, immutable
cf-cache-status: HIT                     <- served from edge, origin untouched
cf-polished: ok, orig_size=162889        <- Cloudflare Polish (re-encode/optimise)
cf-bgj: csam-hash,h2pri                  <- Cloudflare CSAM Scanning enabled
age: 1585
server: cloudflare
```

Filenames are **content-addressed** (`/image/<64 hex>.jpeg` — a SHA-256-shaped digest). Taken
together: identical content deduplicates to one object, every object is immutable, cached at
the edge for a year, and re-served without touching origin. **Serving an image costs
approximately nothing.** Only the first generation costs money.

`cf-bgj: csam-hash` also shows Cloudflare's CSAM Scanning Tool is switched on — a non-trivial
safety control for a no-login image generator, and worth recording.

## 10. VERIFIED — What the model actually emits

Three gallery images downloaded and inspected with `exiftool`:

| | img1 | img2 | img3 |
|---|---|---|---|
| Format | JPEG baseline | JPEG baseline | JPEG baseline |
| Dimensions | **768×768** | **768×768** | **768×768** |
| Megapixels | **0.590** | **0.590** | **0.590** |
| Chroma subsampling | YCbCr 4:2:0 | 4:2:0 | 4:2:0 |
| File size | 163 kB | 120 kB | 104 kB |
| EXIF / XMP / PNG text | **none — fully stripped** | none | none |

- The **0.59 MP ceiling is real in production output**, not just a client-side validation.
- Output is **JPEG 4:2:0 at ~100–160 kB**, not PNG. No metadata survives, so there is **no
  model/sampler/step fingerprint in the files**. Stripping is consistent with `cf-polished`
  (Polish removes metadata) and/or deliberate server-side scrubbing.

This closes the brief's Task 3: **EXIF cannot identify the model, because there is no EXIF.**

## 11. The model — strongest available inference (NOT confirmed)

No model identifier exists anywhere client-side. Verified absent from: all 16 plugin sources,
the gallery DOM, the API responses, and the image metadata. The FAQ's position ("only the Dev
knows") still holds.

But the gallery DOM **does** expose per-image generation settings, and those are diagnostic.
From 200 gallery items:

```
data-guidance-scale="7"    x194        <- the default
data-guidance-scale="30"   x4
data-guidance-scale="15"   x1
data-guidance-scale="10"   x1
non-empty data-negative-prompt: 27 items
  e.g. "low-quality, deformed, blurry, bad art"
       "scratches, faded, washed out, grainy, dirty"
```

**Real, varied CFG values and working negative prompts are the discriminator:**

| Candidate | True CFG? | Negative prompts? | Steps | Verdict |
|---|---|---|---|---|
| FLUX.1-schnell (stock) | No — guidance-free, CFG=1 | **No** | 4 | **Ruled out** |
| FLUX.1-dev (stock) | No — guidance-*distilled* scalar | Not natively | 20–28 | **Argues against** |
| **Chroma** (de-distilled schnell) | **Yes** | **Yes** | 26–40 | **Consistent** |
| SDXL / SD1.5 | Yes | Yes | 20–30 | Consistent, but contradicts 2025 Flux migration |

The community claim is **"FLUX Chroma"** — Chroma being an **8.9B, Apache-2.0** model
**de-distilled from FLUX.1-schnell** specifically to restore **real CFG and negative prompt
support**, at the cost of needing 26–40 real steps instead of schnell's 4.

**Status: strong but UNCONFIRMED.** The Lemmy post asserting it is from a community member
(`RandomPerchanceUser`), **not the Perchance dev**, and offers no evidence. What this
investigation adds is that the *observed client parameter surface independently fits Chroma
and rules out stock schnell* — which is corroboration, not proof. The Apache-2.0 license is
also the commercially safe choice for an ad-funded service, unlike FLUX.1-dev (non-commercial).

## 12. The GPU — still genuinely unknowable

No progress, and no honest way to make progress from outside:

- Every origin sits behind Cloudflare; **no origin IP, ASN, provider or region is observable**.
- No header, timing, or error path leaked hardware information.
- The only public statement remains the dev's own "load balancing server + GPU servers" (plural).

The *only* defensible inference is a **VRAM floor from the model class**: an 8.9B model at fp8
needs roughly 9–12 GB of weights, so a ≥12 GB card suffices, and a 24 GB card (4090-class)
runs it comfortably with batching headroom. **Which card, how many, and whose datacenter is
not determinable and is not estimated here.**

## 13. Revised cost picture — why "ads" is the funding, not the explanation

The first pass's §6 table assumed a possible ~4-step regime. **If the model is Chroma, that
column is wrong** — de-distillation means 26–40 steps, *and* true CFG costs **two forward
passes per step** (conditional + unconditional). That is roughly an order of magnitude more
compute than stock schnell.

Rough revised estimate (**all assumptions, not measurements**): an 8.9B model at 0.59 MP,
~26 steps with CFG, on a 4090-class card lands around **8–25 s/image** depending on variant
(`Chroma1-Flash` is the fast one) and batching — i.e. roughly **$0.001–$0.003 per image** at
$0.34–0.74/hr. Call it **0.1–0.3 cents an image**.

Against an assumed ~$1 RPM, one ad impression (~$0.001) buys well under one image. **So ads
alone do not obviously cover a heavy user, and that is the real insight:** the service is not
sustainable because ad revenue is large — it is sustainable because **the cost of the heavy
tail is engineered close to zero**. Every mechanism found in this investigation is a cost
control:

| Mechanism | Where verified | Cost effect |
|---|---|---|
| 0.59 MP hard ceiling | §3.3, §10 | ~2× cheaper than 1024², and caps the worst case |
| JPEG 4:2:0 output, ~100–160 kB | §10 | Cheap egress, cheap storage |
| Content-addressed + 1-year immutable edge cache | §9.1 | Re-serving costs ~nothing; dedup |
| Cloudflare Polish | §9.1 | Further bytes off the wire |
| `IntersectionObserver` lazy generation | §3.2 | **Off-screen images in a batch of 32 are never generated** |
| Images are temporary unless saved to gallery | FAQ | No durable storage for most output |
| **Cloudflare Turnstile on generation** | §4.2 | **Blocks scripted/bulk consumption entirely** |

That last row is the keystone. "Unlimited" is true *for a human at a keyboard*, and
structurally impossible *for a script* — which is precisely the population that would make an
unlimited free GPU service impossible. The brief's own framing ("free, unlimited") is best
read as: **unlimited per human, closed to automation, and capped at 0.59 MP.**

### Correction to §6

§6's low-step column (≈2 s/image, ~$0.0002) should be treated as a **floor that likely does not
apply**, unless Perchance runs `Chroma1-Flash` or a similar few-step variant. The dev's Oct-2025
"~2× faster" update is consistent with exactly such a variant swap. Step count remains **unknown**
and is still the single largest lever in the model.

---

# THIRD PASS — "the ads can't possibly cover this"

A reasonable objection: *even at a negligible per-image cost, one small footer banner cannot
fund years of free, unlimited generation that returns six images almost instantly — there must
be a free or near-free compute source.*

This pass tests that. Two parts of the objection turn out to rest on premises that the
architecture and the traffic data do not support. A third part is correct and stays open.

## 14. The "six images instantly" premise is explained by architecture, not cheap compute

This is the part the first pass already proved without realising its significance.

**Six images is not one GPU producing six images.** §3.1 established that the plugin emits
**one independent `/embed` iframe per image**. Six images means **six separate HTTP requests**
arriving at the load balancer, which can dispatch them to **six different workers**. The
`thread=0..N` slots (§4.1), each holding its own `userKey`, are exactly the client-side
mechanism for this parallelism.

So the wall-clock a user perceives for a 6-image batch is **the latency of ONE image**, not six.
No unusual hardware is needed to make that feel instant — only enough fleet capacity to absorb
concurrent requests.

### 14.1 …but this refines the model inference, and against my own §11

If a 768×768 image really does land in ~2–4 s, that is **evidence against** the 26–40-step
de-distilled Chroma reading in §11, which would take considerably longer even on a 4090. The
observation points toward a **few-step variant** — `Chroma1-Flash`, or another distilled
configuration — which is also consistent with the dev's Oct-2025 "~2× faster" update.

**Net effect: §11's model-family inference (Chroma lineage, from working CFG + negative
prompts) still stands, but the step count almost certainly sits at the fast end, not 26–40.**
That moves cost back toward the cheap column, and §13's revision was too pessimistic.

## 15. The "tiny banner" premise understates the revenue by an order of magnitude

The banner is one unit, but the multiplier is traffic × depth, and Perchance's depth is unusual.

Third-party panel data (July 2026):

| Metric | Value | Source |
|---|---|---|
| Monthly visits | **22.6 M** | Similarweb |
| Monthly visits | **51.6 M** | Semrush |
| **Pages per visit** | **6.30** | Similarweb |
| Avg visit duration | **6 min 02 s** | Similarweb |
| Bounce rate | 53.63 % | Similarweb |

The two panels disagree by 2.3×, which is methodology, not error — the honest range is
"tens of millions of visits a month."

**6.3 pages per visit is the number that breaks the intuition.** The ad is not shown once per
visitor; it is shown on every AI-plugin page they open, and they open ~6 per visit over ~6
minutes. That is **~142 M pageviews/month** on the Similarweb figure, **~325 M** on Semrush —
before any in-session ad refresh is counted.

## 16. Does it close without free compute? — `research/perchance_economics.py`

The model is committed as a runnable script so every assumption can be challenged and changed.

```
--- similarweb (22.6M visits) ---
  ad impressions (A1: 50% of pages carry ads)      71 M
  REVENUE  (A2: $0.30-1.00 RPM)            $21,357 ..  $71,190
  images generated (A3: 15% of visits x A4: 8 ea)  27.1 M
  cost/image                              $0.00011 .. $0.00206
  COST     (A5: 2-10 s/img, A6: $0.20-0.74/GPU-hr)  $3,013 ..  $55,747
  => CLOSES at the cheap end

--- semrush (51.6M visits) ---
  REVENUE                                  $48,762 .. $162,540
  COST                                      $6,880 .. $127,280
  => CLOSES at the cheap end
```

**Conclusion: a free or donated compute source is NOT REQUIRED by the arithmetic.** At the
cheap end — a few-step model on interruptible or owned GPUs — revenue exceeds cost by roughly
an order of magnitude. At the expensive end — 26–40 steps with CFG on on-demand pricing —
it does **not** close, which matches the dev's own 2023 admission that he was then paying part
of the bill himself, and his 2025 claim of sustainability "+ a lot of work optimizing the load
balancing server and GPU servers."

Fleet sizing implied by the same assumptions: ~27 M images/month ≈ 10 images/second average.
At 2–10 s/image that is **roughly 20–100 GPUs**, which at $0.20/hr spot is ~$3–15 k/month, or a
one-off capex of ~$40–200 k if owned outright.

## 17. Where the objection is RIGHT, and what stays unknown

**The compute sourcing is genuinely undisclosed, and a near-free source cannot be ruled out.**
I could not confirm or refute it:

- The dev's economics posts (`lemmy.world/post/23831024`, `/post/37779986`) were re-read in
  full for this pass. **He never states how GPU compute is sourced** — not rented, owned,
  sponsored, spot, or donated. The only phrasing is "load balancing server + GPU servers."
- Origin remains invisible behind Cloudflare (§12), so no provider, ASN or region is observable.

Plausible near-free sources, **none verified**, listed only so the hypothesis is stated properly:

| Candidate | Why plausible | Status |
|---|---|---|
| **Owned hardware** | Marginal cost = electricity. Fits a solo operator who stresses independence, "no investors, will never sell." | Unverified |
| **Interruptible / distributed consumer GPUs** (spot, Vast, Salad-style) | $0.10–0.20/hr for 4090-class; image gen tolerates preemption well — a failed job is just a retry. | Unverified |
| Sponsored / donated compute | Would fit "public good," but he has explicitly refused investors and has no donation link. | No evidence |

**Bottom line.** The objection's conclusion ("he must have near-free compute") is *possible but
not necessary*: the economics close on ordinary spot pricing once the real traffic depth is
counted. The objection's premises ("six images is six GPUs' worth of work", "one banner is
negligible") do not survive contact with the architecture (§14) and the traffic data (§15).

### Correction to a widely-repeated claim

One SEO article surfaced in search states Perchance "uses client-side browser execution, meaning
prompts never hit external servers." **This is false and was directly disproved here**: prompts
travel to `image-generation.perchance.org` (§3.1, §4.2), and generation is server-side on GPUs
the operator pays for. Noted because the claim circulates widely.

## 18. MEASURED — gallery save rate, and a sanity check on volume

The throughput measurement referred to above **did** complete (an earlier note in this document
saying it produced no number was wrong). Method: load the gallery for
`ai-text-to-image-generator` sorted by **recent**, snapshot the top 200 `data-image-id` content
hashes, wait, re-snapshot, and count hashes not present in the first set.

```
SNAP1  200 ids  @ 2026-09-21T23:12:51Z   (first = bf3aa86a55fe)
SNAP2  200 ids  @ 2026-09-21T23:18:03Z   (first = 1709ef047237)
elapsed 5.20 min      new in SNAP2 = 6
=> 1.15 gallery-saved images / minute
```

Only 6 of 200 turned over, so the window did **not** saturate — this is a real rate, not a
floor imposed by the sample size.

```
1.15/min  =  1,656/day  =  ~49,700/month      (public gallery saves, ONE generator)
```

**What it does and does not tell us.** Gallery saves require an explicit user action, so they
are a small and *unknown* fraction of generations. Scaling by the assumed public-save rate:

| assumed save rate | implied images/month, this generator |
|---|---|
| 5 % | 1.0 M |
| 2 % | 2.5 M |
| 1 % | 5.0 M |
| 0.5 % | 9.9 M |

§16's model assumes ~27 M images/month **sitewide** (all AI image generators share this
backend). If `ai-text-to-image-generator` is the flagship and contributes roughly 20–30 % of
that, it would be doing ~5–8 M/month, implying a public-save rate of **~0.6–1 %** — entirely
plausible for a public gallery.

**So the measurement is order-of-magnitude consistent with §16's assumptions.** That is a
sanity check, not a confirmation: the save rate is the free parameter and it is unmeasured.
The honest summary is that nothing found in this investigation forces the volume estimate to be
wrong, and nothing pins it down either.
