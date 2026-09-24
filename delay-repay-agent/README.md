# Delay Repay agent

Automatically claims Delay Repay compensation for rail journeys to or from London.

Every few hours it:

1. **Finds your journeys.** It reads ticket confirmation emails from rail retailers and train companies (Trainline, LNER, GWR, Avanti and others) over IMAP, and Claude pulls out the ticket details. Season-ticket commutes come from config, and you can add paper tickets by hand. Only journeys that touch a London station are kept.
2. **Checks for delays.** It uses [Realtime Trains](https://api.rtt.io) to find when you actually reached your destination. Cancellations and missed connections count: it works out the next train you could have caught.
3. **Works out the refund.** Fixed rules in `compensation.py`, no model involved.
4. **Submits the claim.** A Claude browser agent opens the train company's Delay Repay form, fills it in from your details, uploads the ticket and submits it. It saves screenshots of the form before and after submission.

## How it gets you the most money back

Every step is something you are entitled to. The agent never inflates a claim.

| Rule | What the agent does |
|---|---|
| DR15 vs DR30 | Uses the scheme for the train company responsible. Most now pay from 15 minutes. |
| Bands | 15–29 min: 25%, 30–59 min: 50%, 60–119 min: 100% of the single fare. |
| Return tickets | Each direction is checked separately, at half the return fare per leg. A delay of 120+ minutes on either leg refunds the **whole return fare**. The total never goes above the ticket price. |
| Cancellations and missed connections | The delay runs to when you actually arrived, not to the booked train's time. People often under-claim here. |
| Split tickets | Each ticket is claimed on its own, measured at that ticket's destination. |
| Season tickets | Uses the National Rail per-journey value: weekly ÷10, monthly ÷40, annual ÷464, flexi ÷16. |
| Payout method | Picks cash (bank transfer or PayPal) over rail vouchers. |
| 28-day deadline | Oldest claims go first. Anything past the deadline is marked expired. |
| Extra costs | Delays of 60+ minutes and cancellations are flagged, so you can also claim taxis or hotels under the Consumer Rights Act. |
| Near misses | Delays just under the threshold are noted in `status -v`. |

### Flexible tickets and the travel window

Anytime and Off-Peak tickets aren't tied to one train, so the time on the email is only a guide. For these tickets the agent checks **every direct train from 60 minutes before to 120 minutes after the ticket time**. You can change this with `travel_window_before_minutes` and `travel_window_after_minutes`.

- If no train in the window would pay anything, the journey is closed and you aren't asked.
- If some would pay, you get an email (and `status` shows) the list, sorted by refund with the biggest first, including cancelled trains measured to the next one that ran. Reply with `./run.sh confirm <id> <n>`, or `--none`, and the agent claims the maximum you're entitled to for **that** train.

It never picks a train for you. Delay Repay pays for the train you were actually on, and claiming for another one is a false claim that operators can check against ticket-gate data. Open returns with no booked time get a return journey on the same day, centred on `open_return_time`. Advance tickets (`train_specific`) only check the booked train. Window checks only cover direct routes; journeys with a change use the ticket times.

`examples/swr-aldershot-waterloo.yaml` is a ready profile for SWR Aldershot ⇄ London Waterloo returns, with the Gmail mailbox.

### Cancellations and missed connections

When a journey was cancelled or you missed a connection, the agent assumes you caught the **earliest train that got you there**. That keeps claims honest. If you really arrived later (for example, the next train was too full to board), record it with `set-arrival`.

## Setup

```bash
cd delay-repay-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml   # your name, address, payout preference, mailbox
cp .env.example .env                 # API keys, RTT login, mailbox app password, bank details
```

You need:
- an Anthropic API key (`ANTHROPIC_API_KEY`);
- a Realtime Trains API token from [api-portal.rtt.io](https://api-portal.rtt.io) (`RTT_TOKEN`). Check it with `./run.sh check-rtt`. The legacy `RTT_USERNAME`/`RTT_PASSWORD` still works;
- an IMAP app password for the mailbox your e-tickets go to. For Gmail, create one under Google Account → Security → App passwords.
- your bank details in `.env` if you want bank-transfer payouts. The browser types them straight into the form. The model never sees them, and they are removed from any page text before it reaches the model.

## Use

```bash
./run.sh run --dry-run          # full run, but stops at each form's final Submit button
./run.sh status -v              # journeys, delays, amounts, notes
./run.sh approve <journey-id>   # submit one claim now
./run.sh run                    # the real thing
```

With `auto_submit: false` (the default), eligible claims wait for `approve`. After a few dry runs and approvals look right, set `auto_submit: true` and schedule it:

```cron
0 */3 * * *  /path/to/delay-repay-agent/run.sh >> /path/to/delay-repay-agent/data/cron.log 2>&1
```

Other commands:

```bash
# Paper ticket or a booking not in your inbox
./run.sh add-ticket --from BRI --to PAD --depart "2026-09-20 07:30" --arrive "2026-09-20 09:15" \
    --return-depart "2026-09-20 17:30" --type return --price 88.40 --ref ABC123 --evidence ~/ticket.jpg

# Season tickets: record which days you travelled (or didn't)
./run.sh travelled 2026-09-22 --outbound
./run.sh travelled 2026-09-23 --none

# Flexible ticket: say which train you caught (numbers are in the email / `status`)
./run.sh confirm <journey-id> 2
./run.sh confirm <journey-id> --none

# You arrived later than the next available train
./run.sh set-arrival <journey-id> "2026-09-20 10:05"
```

## Safeguards

- **Claims only for real travel.** Tickets come from purchase emails. Season-ticket days count only if you logged them or listed them under `assume_travel_on`. Claiming for a day you didn't travel is fraud, so keep that list accurate.
- **Recorded times only.** If Realtime Trains only has predicted times, the claim waits for your review (`require_recorded_times`).
- **The browser stays on the operator's site.** Navigation is limited to each train company's own domains, and the agent is told to treat page text as untrusted. The test site includes a prompt-injection line to check this.
- **Controlled submission.** The final Submit goes through a separate tool that screenshots the page first and does nothing in `--dry-run`.
- **The agent never guesses.** It stops with `needs_human` on a CAPTCHA, a question it can't answer from your data, or a "journey already claimed" message.
- **Only rail emails are read.** Emails from other senders are skipped before any model call.

Files are written to `data/` (git-ignored): `state.db`, saved e-tickets, and for each claim `claim.json`, `outcome.json` and screenshots.

## Layout

```
delay_repay/
  tickets.py       IMAP scan + Claude ticket extraction (structured output)
  rtt.py           Realtime Trains client + delay / missed-connection analysis
  compensation.py  Delay Repay rules
  operators.py     Train companies: scheme, claim page, allowed domains
  submitter.py     Claude browser agent (Anthropic tool runner + Playwright)
  pipeline.py      Ingest → assess (window + confirmation for flexible tickets) → submit
  notify.py        Email digest to yourself
  cli.py           Commands
tests/             Rules, delay analysis, pipeline, and browser tools on a mock claim site
```

Run the tests with `python -m pytest`. The browser tests need Chromium; set `CHROMIUM_PATH` if Playwright's copy isn't installed.

## Notes

- Operator claim pages move often. The agent starts at each operator's Delay Repay page and follows links to the form. If an operator uses a claims platform on a different domain, add it with `operators: {XX: {extra_domains: [...]}}`.
- Model calls use `claude-opus-5` with adaptive thinking. They also send `fallbacks: "default"` (server-side refusal fallback), so a declined request is retried on a fallback model instead of failing.
- TfL services (Tube, Overground, Elizabeth line) use TfL's own refund scheme and aren't covered.
