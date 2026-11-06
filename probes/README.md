# Ember — Phase 0 probes

Two diagnostic scripts. They are **not** part of the dashboard — they exist so the app is
written against your actual account rather than against documentation.

Both are Python standard library only. Nothing to install.

---

## `probe_hive.py` — what you have, and how it looks

Signs in to Hive, then dumps every device with its live state.

```bash
HIVE_USER=you@example.com HIVE_PASS=your-password python3 probe_hive.py
```

What it does:

1. Reads the Hive SSO page for the Cognito pool and client id.
2. Signs in with **SRP-6a**, so your password is never sent — only proof you know it.
3. If Hive texts you a 2FA code, it asks for it. Type it at the prompt.
4. Registers the device with Cognito, so **the code is not asked for again**.
5. Fetches every device and prints the interesting fields.

What it writes:

| File | What it is |
|---|---|
| `hive-session.json` | The tokens and device credentials, mode `0600`. The dashboard reuses this to refresh quietly — no SMS, no password. |
| `hive-nodes.json` | The raw reply from the API, so nothing is lost in the summary. |

Re-running it reuses the saved session and refreshes the token — no password, no code.

---

## `probe_octopus.py` — the gas

Finds the gas meter and works out what the readings actually are.

```bash
OCTOPUS_API_KEY=sk_live_... OCTOPUS_ACCOUNT=A-12345678 python3 probe_octopus.py
```

- `OCTOPUS_API_KEY` — from <https://octopus.energy/your-account/developer/api-keys/>
- `OCTOPUS_ACCOUNT` — the `A-XXXXXXXX` at the top of your bill

What it works out:

- the gas meter point, the meter serial, and the product code (your tariff)
- **the unit the readings arrive in** — m³ or kWh. This is the one that matters, and it
  decides how cost is calculated
- the **native interval** — hourly or half-hourly
- whether the readings are **rounded**, and how many decimal places they carry
- **how far behind the meter is** — gas data arrives later than electricity
- your **unit rate and standing charge**
- a week of daily totals, and the daily average

What it writes: `gas-profile.json` — the settings the dashboard will be seeded from.

---

## Notes

- The probes do not write anything outside this folder.
- `hive-session.json` holds live credentials. It is written with owner-only permissions.
  Delete it and the next run signs in again.
- Nothing is sent anywhere except British Gas / Centrica and Octopus Energy.
