# GPT-SoVITS immediate-upstream license reliance

Amadeus includes a modified GPT-SoVITS v3 inference runtime. The closest
reproducible immediate upstream is RVC-Boss/GPT-SoVITS commit
`9da7e17efe05041e31d3c3f42c8730ae890397f2`, whose repository-level `LICENSE`
grants the MIT License for the software distributed in that tree. The exact
license text is preserved in `LICENSES/GPT-SoVITS-MIT.txt`.

For the public Amadeus source release, the project relies on that immediate
upstream MIT grant for the integrated GPT-SoVITS files and preserves the more
detailed source credits and permissive nested notices recorded in
`LICENSES/GPT-SoVITS-NESTED-NOTICES.md`.

This reliance has two explicit limits:

- It does not assert that `yangdongchao/SoundStorm` independently published
  its repository under MIT. The SoundStorm source repository contains no
  standalone license file; its relationship to the integrated files remains
  visible in their headers and in the nested notice record.
- `GPT_SoVITS/text/cantonese.py` carries a separately verified AGPL-derived
  source history and is excluded from the public Amadeus source archive. The
  unverified Japanese user dictionary, generated caches, optional UVR/AP-BWE
  utilities, model weights, and reference voices retain their separately
  recorded dispositions.

The root Amadeus AGPL-3.0 license applies to Amadeus-owned code
and modifications. It does not replace or narrow the MIT, Apache-2.0, BSD, or
other terms that continue to govern third-party portions. This is a documented
engineering release policy, not a claim that Amadeus can relicense upstream
copyrights or legal advice. If stronger original-source evidence is later
required, this decision must be reopened rather than silently changing the
record.
