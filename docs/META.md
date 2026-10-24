# Valeria One and Meta

**Meta's Business Agent closes the chat. Valeria One runs the business behind it.**

Across most of the world a Facebook page *is* the website and WhatsApp *is* the front desk. Meta's tools
answer the customer inside WhatsApp, Messenger and Instagram. What happens after the chat is still on
paper, in spreadsheets or nowhere: who the customer is, the follow-up next month, the appointment, the
quote, the signature, the payment, the customers who wrote by SMS or email.

Valeria One is that layer — open source (AGPL-3.0), built so its blocks can live inside Meta Business Suite.

## What a business gets

| Block | What it does | Where it meets Meta |
|---|---|---|
| Inbox | WhatsApp, Messenger, Instagram, SMS, email, Telegram, website, anything via webhook — one list | Business Suite inbox |
| One-click connect | WhatsApp Embedded Signup; Pages + Instagram via Facebook Login for Business; one signed webhook routed to each business | Tech Provider model |
| Lead Ads | Facebook/Instagram lead forms become customers, matched and never duplicated | Lead Ads |
| Comment → private message | "precio?" under a post gets one private answer, by keyword rules (Meta's one-reply/7-day rule built in) | Page + Instagram comments |
| AI replies | the business's own key (Gemini free, Claude, OpenAI); answers only from the business's facts, in the customer's language; hands to a person when unsure | complements Business Agent |
| Booking | in the chat (WhatsApp tap-to-pick lists) and on a public page; no double booking; Google Calendar both ways | WhatsApp interactive messages |
| Customers & pipeline | one record per person across channels; pipeline in the trade's words; teams with roles | Business Suite contacts |
| Quotes & e-signature | itemised, the country's tax, SHA-256 fingerprint, signed on the phone | — |
| Payments | the business's own Stripe / Mercado Pago (Pix) / PayMongo (GCash) / Paystack / Razorpay (UPI) / bank transfer | payment links in chat |
| Follow-ups & campaigns | reminders, thank-yous, campaigns — consent, quiet hours, 24 h window and templates enforced | business-initiated conversations |
| Website | a page for businesses with only a Facebook page; contact form into the inbox | Page "Book" / website button |
| Webhooks & API | every event out, signed; full public API with keys | partners, Business Agent Platform |

**Any country, any trade, any language**: 12 country packs (tax, currency, phone format, messaging,
privacy and e-signature law), 9 industry packs, 11 screen languages including right-to-left Arabic.

## Why it is good for Meta

- **More conversations.** Reminders, follow-ups, quotes, payment links and campaigns are business-initiated
  WhatsApp and Messenger conversations — Meta's fastest-growing revenue line.
- **Businesses stay on Meta** for the whole customer lifecycle, not just the first message.
- **The long tail, everywhere.** A salon in Mérida, a repair shop in Manila, a gym in Lagos: the same product,
  in their language and their law, priced for them.
- **Built to Meta's rules.** Reply windows and templates are enforced in code; the AI is business-specific
  (support, booking, sales), never a general-purpose assistant; comment replies follow the one-reply rule.
- **Open source.** Meta's partners and engineers can read every line, run it, and build on it.

## See it

- Live sandbox (fake businesses in 9 countries, nothing real is sent): https://sandbox.valai.mx
- API docs: https://sandbox.valai.mx/docs
- Code: https://github.com/valeriaone/valeria-one

## Contact

Tadewan Pascua, creator of Valeria One — **tad.pascua@valai.mx** · https://www.linkedin.com/in/tad-pascua-a1b38937/
