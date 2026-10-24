# Valeria One and Meta

**Meta's Business Agent closes the chat. Valeria One runs the business behind it.**

Meta's tools answer customers inside WhatsApp, Messenger and Instagram. What happens after the chat is
still on paper, in spreadsheets or nowhere: who the customer is, what they bought, the follow-up next
month, the appointment, the quote, the signature, the invoice, the customers who wrote by SMS or email.

Valeria One is that layer, built so its blocks can live inside Meta Business Suite:

| Block | What it gives a business | Meta surface it fits |
|---|---|---|
| Inbox | every Meta channel plus SMS, email and anything else in one place | Business Suite inbox |
| Customers | one record per person across channels, consent per country law | Business Suite contacts |
| Pipeline | deals in the business's own words (packages, jobs, treatment plans) | Business Suite / Business Agent hand-off |
| Appointments | booking with no double-booking | WhatsApp Flows, Page "Book" button |
| Country packs | language, currency, tax, messaging and privacy law for each market | every market Meta serves |

## Why it is good for Meta

- **More conversations.** Every follow-up, reminder and reactivation is a business-initiated WhatsApp or
  Messenger conversation.
- **Businesses stay on Meta** for the whole customer lifecycle, not just the first message.
- **Every country on day one.** The rules that differ by country (consent, quiet hours, tax, privacy law)
  are configuration, so the same product works in Manila, Mérida, Lagos and Berlin.
- **It respects Meta's rules by design.** Reply windows and templates are enforced in code, and the AI is
  business-specific (support, bookings, sales), never a general-purpose assistant.

## Built for the Meta platform today

- WhatsApp Business Platform (Cloud API): send text, media and templates; signed webhooks; status tracking;
  24-hour window enforced.
- Messenger and Instagram Direct: send and receive with signed webhooks; echoes ignored.
- Webhook handshake (`hub.challenge`) for every Meta channel.

- One-click connection (Tech Provider): WhatsApp Embedded Signup, Facebook Login for Business for Pages and
  Instagram, one signed webhook routed to each business — see [META_SETUP.md](META_SETUP.md).
- Lead Ads straight into customers, matched to existing ones, never duplicated.

Next: comment-to-DM, WhatsApp Flows for booking, catalog sync,
Business Agent hand-off.

## Contact

Tadewan Pascua, creator of Valeria One.
