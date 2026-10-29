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
