# Compliance & Sending Limits

## Unsubscribe / CAN-SPAM

Confirmed 2026-08-20 against Instantly's API docs: unsubscribe is a real campaign field,
`insert_unsubscribe_header: true` (set in `push_to_instantly.py`'s create-campaign
call), not a body merge tag. Because not every email client surfaces the
List-Unsubscribe header to the recipient, every sequence step body also includes a
plain "Reply STOP to opt out" line as a visible courtesy (see `context/messaging.md`).
Include a physical/business identifier line too if Instantly doesn't add one
automatically (CAN-SPAM requirement for commercial email) — confirm current Instantly
defaults in the campaign editor before the first real send.

## Opt-out handling

Instantly handles unsubscribe suppression natively (a lead who clicks unsubscribe is
auto-removed from future steps and future campaigns on that workspace). No custom code
needed — just make sure the link is present.

## Sending limits

This pipeline assumes your mailboxes are already warmed up and configured. It is not
responsible for warm-up, only for not overloading it:

- **Daily send cap per campaign**: start at 20 leads/day for the first real batch (well
  under typical warmed-mailbox capacity), reviewed before scaling past the 5-lead test.
- **Stop-on-reply**: enabled on every campaign — a lead who replies is pulled out of the
  automated sequence immediately (native Instantly behavior).
- Never increase daily send volume without an explicit human go-ahead, per the rule on
  paid and metered actions.

## GDPR note

UK/AU are GDPR/similar-regime relevant even for B2B cold outreach. Keep messaging
legitimate-interest-appropriate (relevant to their business, easy opt-out, no dark
patterns) rather than assuming CAN-SPAM's lighter bar covers all four target
geographies.
