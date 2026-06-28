# Icebreaker — Buddy reaching out first

**One line:** a few random days a week, Buddy opens a conversation with a light, life-aware
message built from things it already knows, no web search, no cost, just a friend saying hi.

**Why it exists:** a companion reaches out sometimes. It makes Buddy feel like a person in
your life, not an app you have to remember to open. It is deliberately low-key and occasional.

---

## How it works

```
the every-minute server tick runs the icebreaker check once an hour
      │
      ▼
is today one of this user's ~3 random "reach out" days, and a good moment?
      │ no → skip
      │ yes
      ▼
build an opener from FREE context only:
   the weather · today's headlines in a category you follow · facts Buddy knows about your life
   (NO web search — this is the cheap, no-cost notifier)
      │
      ▼
send through the proactive lane (low priority, yields to everything you asked for)
```

The key constraint: **free context only**. The icebreaker never does a live web search. If it
needs to look something up, that's the tracker's or chat's job, not a casual hello.

---

## Two worked examples

```
EXAMPLE 1 — weather + something it knows about you
   Buddy knows (from your aura) that you run in the mornings; today's weather says heavy rain
        ▼
   on one of your random reach-out days: "saw it's pouring out there, did your run survive
   or is today a rest day?"
        ▼
   warm, specific to you, and it cost nothing to produce
```

```
EXAMPLE 2 — a headline in a lane you care about
   a headline lands in a category your aura says you follow (say, space)
        ▼
   "did you catch the launch this morning? thought of you"
        ▼
   no web search — it used the free headline feed + your known interest, nothing more
```

---

## Guardrails

```
▸ only ~3 random days a week, and only at a sensible moment → never daily, never spammy
▸ free context only (weather / headlines / known life facts) → zero web-search cost
▸ proactive lane, low priority → a reminder or tracker update always comes first
```

---

## Where this connects

It reads your [aura profile](./user-aura.md) and your known life facts to be specific, and
sends through the [delivery funnel](./notification-delivery.md) (proactive lane, priority 20,
just above raw news). It is the cheap cousin of the [signal engine](./signal-engine.md):
same "Buddy's idea" lane, but personal-opener instead of content.

---

## Code map (for engineers)

```
backend/src/services/icebreaker/
  icebreaker_engine.py   the ~3-days-a-week opener built from free context
backend/src/services/life_facts_schema.py   the "facts Buddy knows about your life" shape
```

## Status (2026-06-22)

Built; the on/off flag was removed so it's always-on in code, but **not yet deployed**. The
client tap-routing for openers is already in place.
