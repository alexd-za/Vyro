# Asset licenses

Every third-party asset bundled with or fetched by this project, per the
project rule: record source, creator, and license; prefer clear commercial
licenses; download instead of hotlinking.

| Asset | Source | Creator | License | How it gets here |
|-------|--------|---------|---------|------------------|
| `assets/vendor/three.min.js` (r158) | https://github.com/mrdoob/three.js | three.js authors | MIT | vendored in repo |
| Anton | https://github.com/google/fonts (ofl/anton) | Vernon Adams | SIL OFL 1.1 | `./clip fx fonts` (not committed) |
| Bebas Neue | https://github.com/google/fonts (ofl/bebasneue) | Ryoichi Tsunekawa | SIL OFL 1.1 | `./clip fx fonts` |
| Archivo Black | https://github.com/google/fonts (ofl/archivoblack) | Omnibus-Type | SIL OFL 1.1 | `./clip fx fonts` |
| Luckiest Guy | https://github.com/google/fonts (apache/luckiestguy) | Astigmatic | Apache-2.0 | `./clip fx fonts` |

Campaign footage is never an "asset" of this repo — only source video the
campaign owner approved may be clipped, and it stays out of git (`work/`,
`out/`, `inbox/` are ignored).
