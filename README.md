# Ember

A self-hosted dashboard for **Hive heating** and **Octopus gas**.

It runs on a Raspberry Pi and shows three slides on a 7" 800×480 panel:
what the heating is doing now, what the gas meter has read, and what
tomorrow is likely to cost.

**Standard library only.** No `pip install`, no build step, no Node. The
whole thing is Python and three files of HTML, CSS and JavaScript.
**Python 3.9 or newer** (it uses `zoneinfo`; nothing else).

**Set it up in the browser.** There is a settings page at `/settings`:
paste your Hive login and your Octopus API key, and Ember works out the
account, the meter, the tariff and the rates for itself. Nothing to
copy between machines and nothing to remember to run first.

---

## The idea in one paragraph

Hive knows the heating. Octopus knows the gas. Neither one knows both.
Hive's app can tell you how long the boiler has been on but has no idea
what the gas cost; Octopus can tell you what the gas cost but has no idea
why. Ember puts them side by side and joins them to the outside
temperature, so you can see **whether a high-gas day was a high-heating
day or something else**.

---

## Running it

```bash
cd ember-dashboard
python3 run.py --demo        # generated data, no account needed
```

Then open <http://127.0.0.1:8766>.

For the real thing, set the environment variables and restart:

```bash
EMBER_OCTOPUS_API_KEY=sk_live_... \
EMBER_OCTOPUS_ACCOUNT=A-12345678 \
EMBER_HIVE_EMAIL=you@example.com \
EMBER_HIVE_PASSWORD=your-password \
EMBER_POSTCODE=SW1A 1AA \
python3 run.py
```

Or write them into the settings file:

```bash
python3 run.py --data-dir ~/.local/share/ember
$EDITOR ~/.local/share/ember/settings.json
```

Every setting can be an environment variable: `EMBER_` plus the setting
name in capitals and underscores. The environment wins over the file.

### Options

| Flag | What it does |
|---|---|
| `--host` | What to bind to. Defaults to `127.0.0.1`. |
| `--port` | Defaults to `8766`. |
| `--demo` | Generated data instead of the real accounts. |
| `--data-dir` | Where the settings and the history live. |
| `--settings` | Path to one specific settings file. |
| `--verbose` | Log every request. |

> The settings page can write your Hive password and Octopus API key, so
> the server binds to localhost by default. `--host 0.0.0.0` puts it on
> the network — only do that on a network you trust. There is no login
> on that page; see [Setting it up](#setting-it-up).

---

## Setting it up

Start Ember, then open **<http://127.0.0.1:8766/settings>**.

The settings page asks for the two things you actually have — your Hive
login and your Octopus API key — and works out everything else by
talking to the services:

- **Octopus.** Your API key is enough. Ember finds your account number
  (the GraphQL API knows which accounts a key can see), the meter point,
  every meter on it, the tariff in force, the rates, and the unit —
  because the meter's own record says what it measures.
- **Hive.** Your email and password. If Hive sends you a two-step code,
  a box appears on the page and you type it in. The phone is the only
  place that code exists, so there is no way around asking for it. The
  session is written where Ember can refresh it unattended from then on,
  and **the page tells you exactly where it put it** — because "signed
  in" and "the dashboard says not signed in" is otherwise a puzzle with
  no answer.

  Ember uses the session even if refreshing it fails: the access token
  is good for an hour, and Hive's refresh endpoint is the flakiest part
  of the whole thing. If the session cannot be used at all, the panel
  says **why** and where it looked, rather than just "not signed in".
- **Your postcode.** Looked up in the UK's own postcode directory
  (postcodes.io) and turned into a latitude and longitude. It needs no
  key, and it does not mind whether you type `PO4 0PL` or `PO40PL`. A
  town name falls back to Open-Meteo's geocoder.

  Open-Meteo's geocoder knows towns but **not** British postcodes — not
  one of them, not even `SW1A 1AA` — which is why Ember does not use it
  for postcodes.

There is nothing to copy between machines and nothing to remember to
run first. The old way — running probes and copying files — is still
there if you want it, but it is no longer the setup.

The page keeps what you have typed in the browser's local storage, so a
reload does not throw the work away. The settings Ember is really using
live in the settings file, written `0600`, and the page always shows
those in preference to the remembered ones. Once a sign-in or a lookup
has worked, the secret is taken back out of the browser — it lives in
the settings file now, and a copy in `localStorage` is only a
liability.

The page also shows **Stored history**: how big the database has got,
how many rows of what are in it, how fast it is growing and how long it
is kept for. That is the answer to "is this going to fill the card?",
with a number rather than a feeling.

**There is no login on the settings page.** It is meant for a private
network, and a login that everyone shares is not a login. If you put
Ember on a network other people can reach, anyone there can read and
change the settings — so keep it on your own.

### The session renews itself, so you should not have to sign in again

Hive's access token is good for an hour, and the dashboard polls every
two minutes for weeks at a time. So the session has to renew itself,
and it does:

- **A token that is still good is used as it is.** No round trip to
  Hive's refresh endpoint every two minutes for nothing.
- **A token that is about to expire is renewed — five minutes early.**
  Renewing *after* the expiry is too late, because the renewal itself
  needs a live token to make.
- **The renewal goes through Cognito's own refresh flow**, not Hive's
  proxy endpoint. Cognito takes the *refresh* token — a long-lived
  credential that lasts weeks, not an hour — with the device key, and
  hands back a new set of tokens. That is how Hive's own app keeps a
  session alive for weeks, and it is the difference between a session
  that renews itself and one that has to be renewed by hand. Hive's
  proxy endpoint is kept as a second try, for the accounts where
  Cognito's flow does not answer.

  (It used to be recorded in these notes that Cognito's refresh flow
  was not available. It is: a bogus refresh token comes back as *Invalid
  Refresh Token*, not as an unknown operation. That note was wrong, and
  it is what sent the renewal down the flakier path.)
- **A renewed session is written back to the session file.** This is the
  one that matters. Without it the dashboard renews, uses the new token
  for a single poll, throws it away, and goes on holding the original
  token — which stops working an hour later, and then never recovers,
  because every renewal is still being attempted with the dead one.
  That is the bug this replaced: a dashboard that went deaf at the top
  of every hour and stayed deaf until somebody signed in again by
  hand.
- **The refresh token is kept when Cognito does not rotate it.**
  Cognito hands back a new refresh token only when it is configured to
  rotate them; when it does not, the old one is still the good one.
  Losing it because the answer had no `RefreshToken` in it would end
  the session at the very next renewal — a session that dies exactly
  one hour later, every time.
- **A 401 is answered with a renewal and one more attempt.** If Hive
  throws the token away for any other reason, the session renews and
  the call is made again — once, so a broken session cannot loop.
- **When the session really is over, the panel says so.** It names the
  time the token expired and tells you to sign in again, rather than
  showing a bare `Unauthorized`.

Signing in again is one click: your password stays in the settings file
and is never sent back to the browser, so the box on the page is empty
every time — leave it empty and press the button.

### And if the refresh token dies anyway, Ember signs in by itself

A refresh token is a **cache**; your password is the truth. The cache
can go stale for reasons that have nothing to do with you — Cognito
revokes it, the account's device tracking changes, Hive changes
something on their side — and then the only way back is to sign in
again. Which is exactly what you would have to do by hand.

**Ember now does it by itself.** When the renewal fails, the dashboard
signs in again with the login the settings page already holds, quietly,
and carries on. The password never reaches the browser: it is in the
settings file, on the server, in a file only you can read.

Four things make that safe:

- **The sign-in presents the device it already knows.** An account with
  two-step verification asks for a one-time code whenever the sign-in
  does not present a device it recognises — so the device key is part
  of the session file, and it is used. If the device turns out to be
  one the account does not know, the sign-in starts again with no
  device at all; the account then treats it as a new device and asks
  for a one-time code, which the settings page can take.
- **The password is hashed with the right key.** Cognito builds the
  hash from a key that belongs to the pool, and the key is part of the
  pool id — *"eu-west-1_SamNfoWtf"* gives *"SamNfoWtf"*. Without it
  every password is wrong, and Cognito says *"Incorrect username or
  password"*, which points at your password rather than at the missing
  key. The key is worked out again from the pool id every time a
  session is loaded, so a session that cannot be renewed can also sign
  itself back in.
- **A failed sign-in is not repeated straight away.** There is a
  fifteen-minute gap between attempts: repeated sign-ins are how an
  account gets locked.
- **The new session is written to disk**, device handshake and all — so
  the next poll starts from a live token rather than from the dead one.
- **The panel says what is needed** when it cannot: *"Hive wants a
  one-time code — sign in from the settings page"*, or *"the Hive login
  is not stored"*, rather than a bare `Unauthorized`.

### Two bugs, in a row

The first one made the renewal fail; the second one stopped the repair.

**One: the file was written too early.**

The sign-in on the settings page does two things in order: the sign-in
itself, and then the **device handshake**, which registers the device
and hands back a fresh pair of tokens — a refresh token that belongs to
that device, and the device key to go with it.

**The session file was written between them.** So the file held the
*older* pair: a refresh token with no device to present. An hour later,
when that token first needed renewing, Cognito refused it — and
refused it in exactly the way a genuinely dead token is refused:
*"Invalid Refresh Token"*, with and without the device key, because
there was no device key in the file to try.

The order is now the other way round: handshake first, file afterwards.

**Two: the sign-in that was supposed to repair it could not sign in.**
Ember was asked to *"Incorrect username or password"* — and the
password was right. The pool's password key was missing: it is worked
out from the pool id, and the pool id was in the session file, but
nothing worked it out again. So the repair hashed the password with an
empty key, and Cognito — quite correctly — said the password was
wrong. The key is now derived from the pool id whenever a session is
loaded.

The repair can still fail for one honest reason: the account does not
know the device in the file. Then the sign-in starts again with no
device, the account asks for a one-time code, and the panel says so.

Four smaller things came out of the same hunt:

- **A sign-in no longer wipes a known device key.** Cognito does not
  always repeat what it already knows, and "the answer did not mention
  it" is not the same as "there is none".
- **Both of Hive's published client ids are tried.** A refresh token
  belongs to the client that issued it, and the wrong one is refused
  with the same unhelpful answer.
- **The device is offered at sign-in.** The same reason as above: an
  unpresented device means a one-time code. The device is presented
  where Cognito asks for it — in the challenge that follows the
  password step — not in the first request, which is a different flow
  and answers with a challenge Ember does not handle.
- **The settings page signs in as the device on file.** It used to
  start from a blank client, which meant every sign-in from the
  settings page needed a one-time code even when the account already
  knew the device.

---

## The two probes

The settings page does the setup. The probes are still worth having: they
are the sharpest way to see what the account actually contains, and they
are what to run when the shape of an API moves.

Hive's sign-in can need a one-time SMS code, and a background server
cannot ask for a code on a terminal — so the probe does that one
interactive step and writes a session file that Ember then refreshes
silently.

```bash
cd probes
HIVE_USER=you@example.com HIVE_PASS=your-password python3 probe_hive.py
OCTOPUS_API_KEY=sk_live_... python3 probe_octopus.py
```

`probe_hive.py` asks for the code if Hive sends one, then registers the
device so it is not asked again. `probe_octopus.py` works out whether
the meter reads m³ or kWh, and what the tariff is.

The Octopus probe finds your account number by itself — the REST API
will not say which accounts a key can see, but the GraphQL API will, so
the probe swaps the key for a short-lived token and asks. If the key
sees more than one account it lists them and asks you to pick; you can
skip all that with `OCTOPUS_ACCOUNT=A-12345678`.

The product and tariff codes come from the GraphQL API too, because the
REST account payload does not carry them. The query was checked against
Kraken's live schema by introspection, so it is the shape the server
actually accepts rather than a guess — gas tariffs are a plain object
there, not a union, and getting that wrong produces a query the server
rejects outright.

**The probe writes the settings for you.** When it finishes it leaves
`probes/ember-settings.json` (mode `0600`) with everything the dashboard
needs — account, meter reference, every meter serial, the product and
tariff codes, the meter's own type and the unit. Copy it to where the
app looks for its settings, or point the app at it:

```bash
mkdir -p ~/.local/share/ember
cp probes/ember-settings.json ~/.local/share/ember/settings.json
```

Then set the Hive session path in that file and you are live.

Neither probe is part of the running app. They are diagnostics, and the
answers they produce seed the settings.

**Where the session goes.** The Hive probe writes `hive-session.json` to
two places: `~/.local/share/ember/` (which is where the app looks first)
and `probes/hive-session.json` next to the probe. If you run the probe
on one machine and the app on another — usually your Mac and the Pi —
copy that file across and put it in either place.

**`python3 run.py --check` tells you what is wrong.** It checks both
accounts, says which session files it found and which are missing,
prints the meter, the product, the tariff and the rates, and lists
anything the dashboard could not reach — then exits without starting
the server. Worth running first when a panel comes up empty.

The probe prints where it wrote it, and it checks its own arithmetic
before it signs in, so a failure never means "your password is wrong"
without also ruling out a broken handshake. The probe and the app share
the same sign-in code, so the two cannot disagree about whether the
handshake is right.

---

## How the numbers are worked out

### Three kinds of number, and they never mix

Ember shows three different sorts of figure, and they are never dressed
up as each other:

| | what it is | where it comes from | how it looks |
|---|---|---|---|
| **measured** | what the meter actually reported | your Octopus meter | teal, no adjective |
| **estimated** | what today should use, given today's weather | Ember's own model, fitted to your readings | plum, and the word *estimated* |
| **forecast** | what tomorrow should use | the same model, plus the forecast | plum, and the word *forecast* |

A measured figure is never replaced by an estimate. When the meter has
reported today, Ember shows what the meter said and nothing else.

### Today's gas is estimated, because the meter has not reported it yet

Octopus does not deliver gas as it happens. Today's readings arrive
tomorrow, so on any given day "today" has no measured gas in it at all
— and a panel that only ever shows what has been measured would be a
panel with a hole in it.

But today is not a mystery. The same degree-day model that forecasts
tomorrow can be run for today: it knows how cold today is, and it knows
what a day like today cost you before. Ember publishes that as an
**estimate**, with:

* the volume, in the unit your meter reads in;
* the band — how wrong it is likely to be;
* the cost, when the tariff rate is known;
* the mean temperature and the degree-days it used;
* the number of days the fit is standing on.

Two things it will not do. It will not use the mean of the hours so far
— a half-finished day is colder than the day it belongs to, so that
would under-predict every time, and the error would be invisible
because the answer would still look plausible. And it will not publish
money when the unit is unknown, for the same reason the measured
figures do not: no unit, no cost.

The estimate is drawn in plum and labelled *estimated*. The measured
fields next to it stay empty, because they are empty: the meter has not
said.

### The week, in cost — and the days the meter has not reported yet

The Today slide's week strip shows **what each day cost**, in big type,
with the gas that produced it underneath in small type and the heating
hours beside it. A day of gas and a day of heat together is what says
whether the gas was the heating.

Two rules sit behind it:

**A day is costed at the rate of that day.** Not today's rate. The
rates endpoint is asked for the last seven days anyway, so a tariff
switch in the middle of a week does not price the days before it at
the rate after it.

**A day the meter has not reported is estimated, not blank.** Octopus
delivers gas a day or two late, so the last two days of the week would
otherwise be empty — and a blank is not a number. Those days get the
same model, the same band and the same plum, with a tilde in front of
the cost: `~£2.58`. When the real readings arrive they take the
estimate's place; an estimate never replaces a reading, and a reading
never replaces an estimate with a guess.

And a day that is only *half* read is estimated too, because a
half-read day is not a cheaper day — the strip compares whole days with
whole days.

### How much history is kept, and does it grow forever

Ember keeps a small SQLite file so it can show heating hours, compare
the week and fit the forecast. It is bounded, and the bound is enforced
once a day:

| table | what it holds | kept for | grows by |
|---|---|---|---|
| `gas` | the meter's readings | **400 days** | 24 rows a day |
| `polls` | the boiler state at each poll | **400 days** | 720 rows a day (one every two minutes) |
| `trv` | which room was asking for heat | **30 days** | a handful of rows a day |

The room log used to be the problem. It wrote a row for every room on
every poll — twelve rooms every two minutes, eight thousand rows a day,
all but a handful of them saying "the Lounge is still off". It now
records a row only when a room's state *changes*, which is the more
useful record anyway: it is the list of moments a room started or
stopped asking for heat. That is a few dozen rows a day instead of
eight thousand.

Four hundred days of gas is a little over a year, so a year-on-year
comparison is possible; past that the oldest readings go. The window is
a setting (`history_days` and `trv_days`) and the settings page shows
the size of the file and how fast it is growing, with a **Tidy up now**
button if you want it done immediately.

`python3 run.py --check` prints the same numbers, so you can ask the
question from a terminal.

### The rest of the arithmetic

**The gas unit: the meter usually says, and Ember admits when it doesn't.**
The readings themselves never carry a unit — the consumption endpoint
returns `consumption`, `interval_start` and `interval_end` and nothing
else. But the **meter record** does: it carries `meterType` (SMETS2 or
SMETS1) and `consumptionUnits` (M3 or kWh), and SMETS2 meters read
cubic metres while SMETS1 meters read kWh.

Ember asks the meter first. When there is no meter type to be found it
falls back to the size of the numbers — a home with gas heating burns
tens of kWh a day, which is a couple of cubic metres, and those two
ranges barely overlap — and it reports what it decided *and why*, marks
it as a guess, and lets you override it: set `gas_unit` to `m3` or `kwh`
in the settings and Ember stops guessing.

**When the unit is not known, Ember publishes no money.** If neither the
meter record nor the readings can say what the meter reads in, there is
no kWh and so no cost, no energy figure, no budget percentage and no
forecast in money. Ember shows a dash and says why. A `0.00` would read
as "nothing spent", which is a different claim from "not known" — and
"the meter reports kWh" would be a claim Ember has no evidence for.

**Ember asks for a month of readings when it starts.** The store begins
empty, and a chart of the week needs a week. Octopus keeps about a month
of readings, so the first fetch asks for the lot; after that only the
last two days are new, and that is all the quick poll needs. Once a day
the whole month is fetched again, which fills any gap the meter left
and picks up readings Octopus revised. Without this, a fresh install
has two days of history and nothing to draw.

**Octopus's readings arrive with a lag, and Ember says so.** A day with
no readings is a day that has not been reported yet, not a day with no
gas in it — so Ember shows a dash rather than `0.00`, tells you how far
behind the meter is, and leaves unreported days out of the forecast fit
rather than putting a zero into the regression.

**A meter exchange is not a fault, and Ember says that too.** When the
meter is replaced, the old one's readings stop on the day it was
retired and the new one can take a fortnight to start reporting. The
graph is blank for a fortnight, and a blank with no explanation looks
like something is broken. Ember notices that the readings stop exactly
on the day the old meter was retired and names the exchange, with the
date — because Octopus backfills the gap afterwards, and the gap is not
missing gas.

While the gap lasts, the panels would otherwise be a row of dashes, so
Ember also shows **the last day the meter actually reported** — the
volume, the kWh and what it cost — labelled as the last reported day.
It is never shown as today's, because it is not today's.

**Gas in m³, kWh derived.** A SMETS2 meter reports m³. Tariffs are priced
in pence per kWh. So Ember converts, using the calorific value — about
11.0 to 11.2 kWh/m³, depending on the gas and the month. Your bill
states it in MJ/m³ (usually 39 to 40); Ember converts with
`kWh = m³ × CV × 1.02264 ÷ 3.6`, the same way Octopus does. A bill value
of 39.5 MJ/m³ works out at 11.22 kWh/m³. It is a setting you can
correct, not a default baked in.

**A day is a sum, never a count.** On a clock change a British Gas day
is 23 or 25 hours long, and the API returns a matching number of
readings. The day is found by local midnight to local midnight, not by
rolling 24-hour windows.

**Today's comparison is like for like.** At two in the afternoon, today
is compared with two in the afternoon on the other days — not with their
whole selves, which would always look bad.

**The forecast has an intercept.**

```
gas_m3 = baseline + k x degree_days
degree_days = max(0, base_temperature - mean outside temperature)
```

The intercept is not optional with a combi boiler. Hot water and cooking
happen on roughly the same days whatever the weather, so they belong in
the constant, not in the heating term. A line forced through the origin
would push that constant use into the slope and over-predict every mild
day — which, in this country, is most days.

The fit is deliberately robust: a single odd day should not throw the
whole model out. The forecast always shows how many days went into it
and how wide the band around it is, and it says *forecast*, not *bill*.

**Heating hours come from Ember's own record.** The Hive API reports the
boiler as firing or not, right now. It does not report "6 hours 20
minutes today" — that is a sum over time, computed in Hive's cloud from
history they keep. So Ember keeps its own history store: every poll
writes the boiler state and the outside temperature at that moment, and
the room log records each time a room changes from asking for heat to
not asking for it (or the other way round).

**Budget tracking is measured against metered gas.** Hive's own Budget
Tracker is a Hive+ feature, and Hive budgets against the boiler's
estimate. Ember budgets against the gas you actually used and the tariff
rate actually in force, so the number is the money you will really pay.

### The forecast tile, and the weather in it

The panel on the right of the Today slide used to be called *Tomorrow* and
show one temperature in a sentence of arithmetic. It now reads like the
forecast on a phone:

- **Today and tomorrow only.** The panel has room for two days, and a
  week of weather on a dashboard whose job is heating and gas is a week
  of noise.
- **A symbol and a word for each.** Open-Meteo publishes a WMO weather
  code for every day; Ember turns it into a symbol and a word — *Clear*,
  *Partly cloudy*, *Rain*, *Snow*, *Fog*, *Thunderstorm*. A forecast
  that says "four degrees" but not "rain" is half a forecast.
- **A code nobody has seen is not guessed.** An unknown code draws the
  plain thermometer and says nothing, rather than a sunny sky it did not
  read.
- **The gas forecast stays**, under a *Tomorrow's gas* heading: the
  plum figure, the cost, the degree-days and the note about what it was
  fitted on.

### What "Energy" meant, and the two tiles that were empty

The Today tile used to have a row called **Energy**, showing something
like *20.1p*. It was not a duplicate of the cost — it was the **energy
part** of the cost, the other half being the standing charge. But
"Energy" reads as a quantity, and *p* reads as a currency, so the row
looked like it was repeating the cost above it.

It is now **Energy charge**, in pounds like the cost above it, so the
two add up: £0.24 + £0.29 = £0.53. The panel says so underneath:
*"Cost today is the energy charge plus the standing charge."*

The two tiles that showed nothing were empty for the same reason:
**Octopus delivers gas a day or two late**, so there is no reading for
today to compare, and no reading for today to find a peak hour in.

- **vs average** now compares the *estimate* when the meter has not
  reported — a **whole day against whole days**, because the estimate
  is a whole day. (Comparing it with the same-time partial average
  would be comparing a whole day with half a day.) The tile is plum
  and says *"estimated, against whole days"*.
- **Peak hour** is the peak of a **typical day** — the average use in
  each hour of a day across the last week. It is always there once
  there is a week of history, and it answers the question a person
  actually has: *when does this house use gas?*

### The heating hours, and the gap between them and Hive's

Hive measures the boiler's run time itself, from its own hub. Ember
counts it from the API, from its own polls — and the two are not the
same measurement, so they will not always agree.

Three things used to make Ember's figure wrong:

- **A gap Ember was not watching was counted as heating.** Each poll
  was assumed to describe the period until the next one, so a
  six-hour break with the boiler on at the start was counted as six
  hours of heating — hours that may never have happened. It is no
  longer counted.

  **How long is a gap "not watching"? Not a constant.** The first
  version of this rule used a fixed five-minute ceiling, and that
  broke the live dashboard completely: when the Hive session needed
  renewing, every poll took minutes, every interval looked longer
  than the ceiling, and **every heating figure came out zero**. The
  ceiling now comes from the data — the **typical** gap between the
  polls (the median, which ignores both an odd fast poll and the one
  long break) times a generous factor, with a floor under it.

  The factor is generous on purpose, because the two mistakes are not
  equally bad: skipping a watched interval puts the heating to zero,
  while counting a break only over-counts a little.
- **A single poll was counted as heating for the rest of the day.**
  One reading at midnight says the boiler was on at midnight. It says
  nothing about the rest of the day.
- **The figure did not say how much of the day had been watched.** A
  day Ember watched for four hours looked exactly like a day it watched
  for all of it.

So Ember now publishes, for every day, **how much of it it was
watching** — and says so where it matters:

- **"Heating today" and "Heating this week"** read *"at least 2h 00m"*
  when the count is a lower bound rather than the whole truth.
- **The week's tiles** mark a partly-watched day with a **+**, and the
  note under them says how many days are like that.
- **The panel's source line** says the hours are counted *"only while
  it was watching"*.

The count is still a sample — the boiler's state is only known at the
moments Ember polled it, so a run shorter than the poll interval can
be missed. At two minutes that is not worth worrying about.

### The costs, and the difference from the Octopus app

Ember's weekly costs and the Octopus app's are **not the same kind of
number**, and that is the whole difference:

- **Octopus's app shows an estimate.** It says so. Octopus delivers
  the gas readings a day or two late, so for a day it has not measured
  yet it works out what the day probably used — from the weather and
  your own pattern — and prices that.
- **Ember shows the metered cost** for the days the meter *has*
  reported: the meter's own reading, converted at the tariff rate in
  force on that day, plus the standing charge. For the days the meter
  has not reported — which, after a meter exchange, is every day since
  the exchange — Ember shows its own estimate, in plum, and says so.

So on a reported day Ember's figure is the real one and the Octopus
app's is a guess; on an unreported day they are two different
estimates. Ember marks which is which: plum and "est" on the estimate,
and a note under the week saying how many days are still waiting.

**One thing moves Ember's number**: the calorific value. Ember
converts cubic metres to kWh with the value from your settings — 11.16
kWh/m³ by default, which is a guess. Your bill states it in MJ/m³
(usually 39 or 40), and the real number is worth a percent or two of
your gas. Set it and the costs are as good as the meter's.

The panel also says **which unit the readings are in and why**, so the
numbers can be argued with.

### Where the week's heating hours come from

The week's tiles show how long the heating ran each day. **That is the
time the boiler was actually on** — counted from Ember's own polls of
Hive's boiler state, not read from a schedule, and not the hours the
schedule allowed. The panel says so: *"heating is the time the boiler
was actually on"*.

Two details that make the difference:

- **A day Ember was not running says "not recorded".** It does not say
  "0h 00m", because a zero is a claim — and the claim would be "the
  heating never came on", which is not what happened. This is the same
  rule as everywhere else: a zero is a claim, an absence is an absence.
- **The figure is sampled, so it is an estimate.** The boiler's state
  is only known at the moments Ember polled it, and each poll is
  assumed to describe the period until the next one. Two minutes of
  resolution, on a figure that is tens of hours.

The demo models the same thing honestly: its boiler runs *until the
house is warm*, which takes longer on a cold day, so the demo's
heating hours vary from day to day — as a real house's do, and as a
schedule's would not.

### The gas chart: a day, a week, a month

The 24-hour bar chart was the wrong shape for this data. Octopus
delivers gas a day or two late, so "the last 24 hours" was usually
empty — and on the days when it was not empty, it was showing a day
that ended two days ago.

There are three views now, and the tabs switch between them. Each one
names itself in the panel heading, because a panel that says "last 24
hours" and shows something else is a lie:

| Tab | What it shows | Where it lives |
|---|---|---|
| **DAY** | the *shape of a day*: each hour's **average** use across the last week | the gas panel, and the **Today panel** |
| **WEEK** | **a week of days**: one column per day, the week's average as a dotted line, today's estimate in plum | the gas panel (the default) |
| **MONTH** | **the month**: a calendar of the last 30 days, shaded by usage | behind the MONTH tab |

The week view is the gas panel's default because "how much" is what the
gas slide is for. The shape of a day is on the Today panel because
"when does this house use gas" is the question that panel was missing —
the cold morning and the warm evening are visible at a glance.

**One rule runs through all three: a day the meter has not reported is
never drawn as a zero.** Zero means no gas was used, which would be a
lie. What the day *is* depends on what Ember can honestly say about it:

- **The week view estimates it.** The model has an answer for a day
  nobody reported — the same model that forecasts tomorrow — so the
  column is drawn in **plum**, with the band's whisker over the top.
  A day with *part* of a reading keeps that part in teal, faded,
  under the estimate: the part is real, the rest is a guess, and the
  two are never the same colour.
- **The month view leaves it a hole.** The calendar's job is to show
  the pattern of the month, and the pattern here *includes* the meter
  exchange. A dashed cell says "the meter sent nothing here", which is
  a different fact from "about two m³ was used here".
- **The forecast fit never learns from it.** A day the meter did not
  report is not a day of no gas, so it is left out of the fit rather
  than dragging the baseline down.

So the two views are not contradicting each other: the week chart is
showing *how much gas each day took*, and the month grid is showing
*what the meter actually said*.

That last part needed a fix to the history store. A day used to count
as "complete" as soon as later data had arrived — which is true for a
meter that keeps reporting, and wrong for the days after a meter
exchange, when the *new* meter's data moves the date forward while the
day itself holds almost nothing. A day is complete now when the meter
has reported **a row for every hour of it** (two may be missing; an
odd half-hour does go missing now and then). Octopus returns a row for
every interval, zeros included, so the count is meaningful.

The charts are drawn with the real numbers on the design page
(`design/index.html`, section 03).

### The heating panel's rings, and the two pills

The heating panel is three rings: **home** in the middle, the **boiler**
and the **outside** in the corners, and the **boost** underneath the
outside ring. The symbols in them have to mean what the ring beside them
is saying — a flame for a boiler that is firing, a snowflake for a cold
morning, a sun for a warm one, a plain thermometer for anything in
between. A drop where a flame belongs is a picture arguing with the
number next to it. The boost ring counts the minutes that are actually
left, not a hardcoded half hour.

**The boost ring shows whose boost it is.** A boost can be on the boiler
or on one room's valve, and they are different things: the boiler's
lifts the whole house to a higher target, a room's holds that one valve
open for extra heat. The ring shows whichever one ends soonest and
labels it — *BOOST · KITCHEN* — because a ring that says "BOOST" and
does not say what is boosted is a puzzle. The status tile's "Boost" row
went at the same time: it said the same thing a second time, and worse.

The symbols are 20 points now — a third bigger — and they still sit
inside their rings, with the word below them.

**Nothing joins the rings up.** The dotted lines between them said
"these are connected", which the layout already says, and they ran
straight through the numbers. The home ring is also bigger than it was
— the temperature was nearly touching its edge — and the boost ring
moved out from under it, where it crowded the home ring's label.

The status tile's **"Next schedule block"** row is gone. Ember does not
read Hive's schedule, so it could only ever show a dash — and a row
that cannot be filled is a row that has to go. In its place: **"A day,
on average"**, the mean heating time over the last seven *whole* days
(today is half a day, so it is left out — comparing half a day with
whole days would always look cheap). The demo used to invent a "22:30"
block for the sake of a fuller-looking tile; a made-up time is a lie,
and the demo is the first thing anybody looks at.

The bottom of the rooms list is a pair of **pills** too: a room asking
for heat gets an ember-coloured one (*5 of 9*), and the valve count
(*12 valves*) gets a quiet grey one. A plain number in a list of plain
numbers is easy to miss; the one that says somebody is waiting for heat
should not be.

The corner has **two pills**, because they answer two different
questions:

- **DEMO / LIVE** — whether Ember is talking to the real accounts. The
  LIVE pill means "signed in, and the last poll came from Hive and
  Octopus".
- **the clock** — the weekday, the date and the time, ticking every ten
  seconds so it stays honest between polls.

The time used to sit inside the LIVE pill, which meant the pill said
two things at once and neither of them clearly.

### The timestamps Octopus sends

Octopus is not consistent about the timezone it writes. Most rows come
back as `2026-11-03T00:00:00Z`; the older settlement rows come back as
`2026-10-11T17:00:00+01:00` — the same moment, written the way that
day's data was. Both are absolute, so both are read and both become
UTC, and a reading is bucketed into its **local** day either way.

This only appeared once the month-long backfill was added, because the
two-day fetch never asked for the older rows. One unreadable row used
to take the whole application down with it; now a row that cannot be
placed in time is left out of the history and the panel says how many
were skipped. **A reading at the epoch is not an absence — it is a lie,
and it would sit in the database forever.**

---

## Layout

```
ember-dashboard/
├── run.py                  the entry point
├── ember/
│   ├── config.py           settings, with secrets kept out of the browser
│   ├── history.py          the SQLite history store
│   ├── hive.py             Hive: SRP sign-in, nodes, valves
│   ├── octopus.py          Octopus: meters, consumption, tariffs
│   ├── weather.py          Open-Meteo + postcodes.io, no key needed
│   ├── forecast.py         the degree-day model
│   ├── discover.py         working the settings out from a key
│   ├── dashboard.py        the polling loop and the payload
│   ├── demo.py             generated data, for before there are accounts
│   ├── server.py           HTTP and Server-Sent Events
│   └── web/                index.html, settings.html, app.js, theme.css, fonts/
├── probes/                 the two diagnostics
└── tests/                  the test suite
```

---

## Tests

```bash
cd ember-dashboard
python3 -m unittest discover -s tests -v
```

The layout tests need Playwright and a Chromium; where they are not
available those tests skip rather than fail, so the rest still runs.

---

## The Raspberry Pi

```bash
python3 -m http.server --help   # not this one - Ember is its own server
```

Ember serves itself. On the Pi:

```bash
cd ember-dashboard
python3 run.py --host 0.0.0.0 --port 8766
```

Then point the kiosk browser at it. The panel is 800×480, which is
exactly the canvas Ember was drawn at, so it scales by 1 and is
pixel-perfect.

```bash
firefox-esr --kiosk http://127.0.0.1:8766
```

The slides rotate on their own. `/?slide=gas` pins one slide, which is
handy for a bookmark.

Each slide is dealt onto the panel like a card: the new one slides in
from the right and the one it replaces fades away underneath it. Going
backwards, it comes in from the left. A new reading for the same slide
does **not** move anything - the panel is rebuilt every two minutes, and
a screen that is never still is a screen nobody can read.

The header stays where it is while all that happens. It carries the
clock, and it is the one part of the screen that does not change.

**Swipe it.** Drag the panel to the left for the next slide, or to the
right for the one before - the same as the arrow keys. A drag has to be
longer than 60px and clearly across rather than down, so reaching for a
control does not change the slide by accident.

### Running it as a service

```ini
# /etc/systemd/system/ember.service
[Unit]
Description=Ember
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gosdenj
WorkingDirectory=/home/gosdenj/ember-dashboard
ExecStart=/usr/bin/python3 run.py --host 0.0.0.0 --port 8766
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ember
```

> On a unit that is only pulled in by `graphical.target`, do not add
> `After=multi-user.target`: it will sit in `Active: inactive (dead)`
> with a `Job:` line, which means *queued*, not crashed. Enable it in
> both targets instead.

### In a container

Ember is standard library only, so there is nothing to install in the
image and nothing to compile:

```bash
docker compose up -d --build
```

The settings, the history and the Hive session live in the `ember-data`
volume, so they survive a rebuild and a redeploy.

To have Portainer deploy it from this repository instead - building the
image on every push and rolling it out - see
**[docs/PORTAINER.md](docs/PORTAINER.md)**.

---

## Privacy

Read-only. Ember never writes to Hive and never changes your heating.

Your credentials live in one file, written `0600`, and they are never
sent back to the browser. The `/api/settings` endpoint returns
everything except the secrets, and only reports whether a secret *is*
set. The settings page shows what you have typed — which is the one
place a secret is in the browser, because that is where you typed it —
and a button that makes the browser forget it again.

**No endpoint hands back a credential.** Not the password, not the
Octopus API key, and not the Hive tokens: the sign-in route returns a
whitelist of what the page may show, and the refresh token — a
long-lived credential for the Hive account — never leaves the process.
The settings file and its temporary copy are both created `0600` from
the start, because the password is in plain text in both.

The settings page has no login. It is meant for a private network, and
a shared password is not a login. Anyone who can reach it can change
Ember's settings, so keep it to a network you trust.

---

## Licence

MIT.
