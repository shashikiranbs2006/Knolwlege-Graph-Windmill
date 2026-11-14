# Penmanshiel data (farm B, use case 2)

Source: Cubico Sustainable Investments, [Penmanshiel wind farm data](https://zenodo.org/records/16807304), DOI 10.5281/zenodo.16807304. License: CC BY 4.0. Attribute Cubico and cite the Zenodo record.

Files used (download them into this folder; do not unzip):

| File | MD5 seen on first load |
| --- | --- |
| `Penmanshiel_SCADA_2016_WT01-10_3107.zip` | `3819e1b2168b84da195b64e1e9d78b03` |
| `Penmanshiel_SCADA_2016_WT11-15_3107.zip` | `92c30453d43a38e985af3e2e305eedd4` |
| `Penmanshiel_WT_static.csv` | `c4cd4191234c1a67a391fe5d2978256b` |

14 Senvion MM82 turbines (T01–T15, no T03). The 2016 files start between June and July 2016. Loaded: 974,522 ten-minute values (power, energy export, wind speed) and 19,762 status events.

Data notes:
- **Gear oil temperature is `NaN` for the whole of 2016 at Penmanshiel**, so farm B publishes no gear-oil observations. This is a real gap in the source, and a useful example of "data scarcity and heterogeneity" from the paper.
- `prepare_farm_b.py` loads the data into farm B's *own* table layout (`wtg`, `tenmin` in long format with signal codes `P_avg`, `E_exp`, `WS_avg`, `alarm_log`, `work_orders`), which is deliberately different from farm A. Only `farm_b/mapping.obda` translates it to wind-core.
- Work orders are **synthetic**: generated from real alarm events with a farm-B repair policy that was designed to differ from farm A. Any cross-farm difference in repair actions is therefore illustrative, not a finding. They are labelled `synthetic_demo`.
