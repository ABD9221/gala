"""Perchance unit economics — a transparent, parameterised model.

Every input is a labelled assumption. Nothing here is a measurement of
Perchance's actual bill or revenue; the only hard numbers are the traffic
estimates (third-party panels) and the 0.59 MP output cap (measured).

Run:  python research/perchance_economics.py
"""

# --- MEASURED / THIRD-PARTY ------------------------------------------------
VISITS = {"similarweb": 22.6e6, "semrush": 51.6e6}   # monthly visits, Jul-2026
PAGES_PER_VISIT = 6.30                               # Similarweb
# Measured this investigation: every gallery image was 768x768 = 0.590 MP.

# --- ASSUMPTIONS (the honest part) ----------------------------------------
AD_PAGE_SHARE = 0.50      # A1: share of pageviews on AI-plugin pages (ads only fire there)
RPM = (0.30, 1.00)        # A2: $ per 1000 impressions. AI-image is a low-value, brand-unsafe category.
GEN_VISIT_SHARE = 0.15    # A3: share of visits that actually generate images
IMAGES_PER_GEN_VISIT = 8  # A4: mean images per generating visit
SEC_PER_IMAGE = (2.0, 10.0)   # A5: per-image GPU seconds at 0.59 MP.
                              #     low  = few-step variant (Chroma1-Flash / schnell-class)
                              #     high = de-distilled Chroma, 26-40 steps WITH CFG (2 passes/step)
GPU_HR = (0.20, 0.74)     # A6: $/GPU-hour. low = interruptible/spot or owned-hardware
                          #     equivalent; high = RunPod 4090 on-demand.

def money(x): return f"${x:,.0f}"

print("=" * 74)
print("PERCHANCE UNIT ECONOMICS  (all figures MONTHLY, all inputs assumptions)")
print("=" * 74)

for src, visits in VISITS.items():
    pv = visits * PAGES_PER_VISIT
    imps = pv * AD_PAGE_SHARE
    rev_lo, rev_hi = imps / 1000 * RPM[0], imps / 1000 * RPM[1]

    images = visits * GEN_VISIT_SHARE * IMAGES_PER_GEN_VISIT
    # cost per image = $/hr / (3600 / sec_per_image)
    cost_lo = images * (GPU_HR[0] / (3600 / SEC_PER_IMAGE[0]))   # cheap GPU + fast model
    cost_hi = images * (GPU_HR[1] / (3600 / SEC_PER_IMAGE[1]))   # dear GPU + slow model

    print(f"\n--- traffic source: {src}  ({visits/1e6:.1f}M visits) ---")
    print(f"  pageviews                {pv/1e6:>8.0f} M")
    print(f"  ad impressions (A1)      {imps/1e6:>8.0f} M")
    print(f"  REVENUE  (A2 {RPM[0]}-{RPM[1]} RPM)   {money(rev_lo):>10} .. {money(rev_hi):>10}")
    print(f"  images generated (A3,A4) {images/1e6:>8.1f} M")
    print(f"  cost/image               ${GPU_HR[0]/(3600/SEC_PER_IMAGE[0]):.5f} .. ${GPU_HR[1]/(3600/SEC_PER_IMAGE[1]):.5f}")
    print(f"  COST     (A5,A6)         {money(cost_lo):>10} .. {money(cost_hi):>10}")
    verdict = ("CLOSES at the cheap end" if cost_lo < rev_hi else "DOES NOT CLOSE")
    if cost_hi < rev_lo: verdict = "CLOSES comfortably"
    print(f"  => {verdict}")

print("\n" + "-" * 74)
print("""CONCLUSION
  The model closes WITHOUT any free or donated compute -- but only in the
  cheap corner: a few-step model on interruptible/owned GPUs. In the
  expensive corner (de-distilled Chroma at 26-40 steps with CFG, on-demand
  pricing) revenue does NOT cover cost, which is consistent with the dev's
  2023 statement that he was paying part of it himself.

  So a near-free compute source is NOT REQUIRED by the arithmetic. It also
  cannot be ruled out: sourcing is undisclosed and invisible behind
  Cloudflare. The single biggest unknown remains step count.""")
print("-" * 74)
