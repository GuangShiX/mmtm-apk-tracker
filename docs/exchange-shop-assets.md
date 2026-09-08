# Exchange shop art

The `helper-exchange-shop-chrome` and `helper-exchange-shop-tabs` groups register the exact sprites used by the React shop card master.

- Base package: `ExchangeMainViewController` (`f6a4eac7c61dc03ff0c7a7b678294dae` in 4.20.0) resolves `base_l_01`, `button_l_01_orange`, `tab_bg`, and `ItemIconCursor_*` through its serialized Image references.
- `base_l_01`: 90 x 92, Unity border left 39 / bottom 40 / right 38 / top 40. The product panel uses the original gray tint (0.784, 0.769, 0.749).
- `button_l_01_orange`: 190 x 69, border 13 on each side. Use a nine-slice renderer, not a stretched border screenshot.
- Official 4.21.0 Addressables catalog `4a851fa27b84c8c1a05e57608a16c6486c404219_2`: exact `UI/Exchange/TabIcon/TabIcon_*`, `TabDecoration/TabDecoration_01` through `05`, and `TabSpecial/TabSpecial_01` keys. No wildcard publication.
- `TradeShopTabMB` owns IconId, DecorationId and DecorationColor. Store = icon 1, Boutique = 13, current cave exchange = 60. Consumers must use a verified mapping and a safe fallback when future art is not yet curated.

Package validation requires only package-delivered UI. Final repository validation still requires every registered asset; Addressables assets are checked after the hot-update merge. This prevents both false APK failures and incomplete final publication.

Run `python -m unittest discover -s tests -v`, then the existing `fallback.py --auto-update` flow. The asset repository CI owns publication of PNGs and manifests. React imports the generated output through its manifest-verifying `tools/sync-game-assets.mjs`.
