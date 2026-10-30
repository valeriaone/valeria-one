# Your business's back office, on your own server, in 15 minutes

Your customers already write to you on WhatsApp, Instagram, Messenger, Telegram, by text and by email.
This guide sets up **Valeria One**, an open-source back office that puts all of those conversations in one
inbox and runs what comes after the chat: customers, bookings, quotes, payments, reminders. It runs on a
server you own, it works without AI, and it speaks 11 languages.

You need: a server with Ubuntu 22.04 or newer (1 CPU, 2 GB of memory is plenty) from any provider, a domain
name (WhatsApp and Telegram only deliver to HTTPS addresses, and the domain gives you one), and 15 minutes.

## 1. Install

Log in to your server as root (or with `sudo`) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/valeriaone/valeria-one/main/deploy/install.sh | sudo bash
```

It installs Docker, the database and Valeria One, starts everything, and ends with a line like:

```text
Create the first account here (this link works once):
http://203.0.113.10/setup?k=3f9c…
```

Lost it? `sudo valeria-one setup-link`.

## 2. Create your account

Open the link. Enter your business's name, your country (it brings your currency, tax, phone format and
the messaging and privacy rules that apply where you are), your email and a password. This first account
also runs the server.

Nobody else can sign up: your team joins by invitation (**Settings → Team**).

## 3. Switch on what you use

You land on **Start**:

1. **What you use**: calendar and booking, pipeline, payments, quotes, follow-ups, Grow (customers back +
   bring a friend), website, comment replies, AI answers. Untick what you don't need; it disappears and stays quiet. You can change this any time.
2. **Your words**: call your customers *clients*, *members* or *guests*, call your deals *orders*, *jobs* or
   *projects*, and rename every stage of your pipeline.
3. **Next steps**: what is left to set up, ticked off as you go.

## 4. Your domain, with HTTPS

Point your domain's DNS `A` record at the server's IP, then:

```bash
sudo valeria-one domain office.example.com
```

The certificate is issued and renewed by itself.

## 5. Connect a channel

**Channels** lists the channels used in your country first. The fastest one to try is Telegram: create a
bot with @BotFather and paste its token; Valeria One tells Telegram where to deliver by itself. Write to
your bot from your phone, and the message lands in the **Inbox**. WhatsApp, Messenger and Instagram connect
through Meta ([docs/META_SETUP.md](META_SETUP.md)); SMS through Twilio; email with your own mailbox;
anything else through a signed webhook. TikTok direct messages connect with TikTok's own sign-in once the
server has a TikTok app ([docs/TIKTOK_SETUP.md](TIKTOK_SETUP.md)).

## 5b. Be found, answer reviews, post

- **Directory** (Settings → Directory listing): tick *Show my business in the directory*, write your city and a
  line about you, and choose what to show (hours, phone, WhatsApp, address — nothing personal unless you tick
  it). People nearby find you at `/directory`; what they write arrives in your Inbox as a new customer. It is
  off until you switch it on.
- **Google profile**: connect your Google Business Profile ([docs/GOOGLE_SETUP.md](GOOGLE_SETUP.md) for the
  server's part). Your Google reviews show up within 30 minutes; answer them yourself, or let the AI draft an
  answer that you read and approve — nothing goes to Google without your OK. You also see your calls, website
  clicks, directions and views of the last 30 days.
- **Posts**: type an idea (or pick an offer, something new, a holiday, a review to highlight), let the AI write
  it for Facebook, Instagram and Google (or write it yourself), add a picture, then **Approve and publish now**
  or pick a time. A post that doesn't go out says why, calmly, and **Try again** only retries where it failed.

## 5c. Valeria, your assistant on your phone

Open **Valeria** (or **Start → Talk to Valeria from your phone**): three questions, one per screen.

1. **Link your phone**: tap *Link my phone* and send the code (`V-123456`) from your own phone to your business's
   WhatsApp or Telegram — or type your number. From then on what you write there is for Valeria, never a customer.
   Add your team the same way and tick what each person may ask (see bookings, answer customers, move bookings,
   promos, payment links).
2. **How should Valeria sound?** Formal, friendly or super casual. She learns the rest from your changes to her drafts;
   ask her "how do I sound?" or say "more formal", "no emojis".
3. **Want someone to set it all up for you?** Yes shows up to 3 local helpers; nothing about you is shared until you
   tick *I agree*. The helper may charge for their own work — you decide.

Then just write to her: "today", "anything new?", "who owes me?", "move Luis to 4pm", a photo to post, a voice note.
For anything that goes to a customer she asks *1 yes · 2 no · 3 later*; in **Valeria → What she may do by herself** you
can let her answer customers, ask for reviews, send loyalty messages, move bookings or block times on her own (money,
prices, promos and cancellations always ask). Approved messages wait one minute: **undo** stops them, **Valeria, stop**
pauses everything she does alone until **resume**. Also on that page: the 08:00 summary and Monday proof, quiet-day
offers, holidays (switch off the ones you don't want), reminders with "reply 1 to confirm, 2 to change", loyalty
("every 5 visits: 10% off", off until you set it) and 24/7 answers without AI (prices, hours, address and your booking
link — anything else is "we'll get back to you", and she asks you).

Voice notes need your own AI key (Settings → AI: Google Gemini's free key listens to them); without one they still
arrive, marked "voice note".

Server admins: helpers sign up at `/helpers`; approve them in **Server → Helpers**.
The background jobs (`python -m valeria_one.jobs all`, or `assistant` alone) send what was approved, her questions,
the summaries and the holiday and quiet-day questions, each business in its own time zone.

## 5d. Your own fields, automations, the board and forms

- **Your own fields** — Settings → *Your fields* → *Add a field*: its name, the kind of answer (words, number, money,
  date, yes/no, pick one, phone, link), and whether it shows in the list (deals: also on the board's cards). Fill them in
  on the customer's or deal's page — each one saves by itself. Search and filter the lists by them; they are in
  *Download CSV*, and *Bring customers from a file* fills them by column name. Deleted by mistake? *Undo* for 30 days.
- **Automations** — open *Automations*, tap *Switch on* on a ready-made one, or *Make your own*: When → Only if →
  Then (up to three). *What happened* shows each run in plain words. A message to a customer becomes Valeria's question
  to you unless you untick *Ask me before sending*; it still follows consent, STOP and WhatsApp's 24-hour window.
  They run while Follow-ups is switched on; "no answer for N hours" runs with the background jobs (`recipes`).
- **The board** — Pipeline and Tasks have *Board* and *List* (your phone remembers which). Drag a card (on a phone,
  from its ⠿ grip), or tap *Move to…*; with a keyboard, go to a card and press ← →. Moving a deal sets off its
  automations.
- **Forms** — *Forms* → *New form*: questions, thank-you words, the stage and tag a new lead lands in. Then share
  the link on WhatsApp, paste the embed on your own website, or tick *Show it on my website*.

## 5e. Campaigns, workflows, the phone agent, invoices, ready setups, the app (v0.18)

- **Ready setups** — *Start* → *What kind of business? Set it all up for me* → pick yours → you see exactly what is
  added (what you already have is left alone) → *Set it all up*. Changed your mind? *Undo* for 24 hours.
- **Campaigns** — *Campaigns* → WhatsApp, Text message or Email → *Who* (leave it empty for everyone who said yes to
  offers; the page counts who gets it and who is left out) → *Message* (or *Write it for me*) → *Check* (the email as
  they'll see it; *Send me a test*) → *When*. *How it went* shows the numbers. **Sequences** are on the same page.
- **Workflows** — *Workflows* → *Use this* on a ready-made one, or *New workflow*: pick *When this happens…*, then
  *+ add step* where you want it; an *If / else* shows Yes and No side by side (stacked on a phone). Tap a step to
  change it. Pick the goal at the bottom, save, switch on. *Who went through it* shows each customer's run in words.
  Background jobs: `workflows` and `sequences` (both in `all`).
- **Phone agent** — Settings → *More tools* → *Phone agent*: it asks for your Twilio number (the SMS channel) and your
  AI key first, then one question per screen. Call your number to try it.
- **Invoices** — on a customer's page (*New invoice*) or *Invoices*; accepted quotes have *Make the invoice*. Plans and
  subscriptions are on the *Subscriptions* tab. Background job: `invoices`.
- **The app** — open Valeria One on your phone and tap *Install* (iPhone: Share → *Add to Home Screen*); Settings →
  *Notify me on this device*.

## 6. Backups and updates, handled

- A backup is made every night and before every update. Download copies from the **Server** page.
- When a new version is out, the Server page says so: **Update now**. A backup is made first, and if the new
  version does not start, the previous one comes back by itself, with your data as it was.

```bash
sudo valeria-one status     # what is running, version, last backup
sudo valeria-one help       # everything else
```

## 7. AI, only if you want it

Everything above works without AI. To let AI draft or send replies, open **Settings → AI answers** and paste
your own key (Google Gemini has a free tier; Claude and OpenAI work too). It answers only from your own
services, prices and hours, in your customer's language, and hands the chat to you when it is unsure.

## 8. Running it for others

Agencies and IT people: **Server → Add a business** creates a business for a client and gives you an owner
invitation link. Hosts with their own control panel and billing: the [Partner API](PARTNER_API.md) creates,
pauses, resumes and bills businesses.

---

Valeria One is open source (AGPL-3.0): https://github.com/valeriaone/valeria-one · Created by Tadewan Pascua ·
tad.pascua@valai.mx
