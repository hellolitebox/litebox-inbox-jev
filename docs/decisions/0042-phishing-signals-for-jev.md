# 0042. Give Jev phishing signals; passing authentication is not legitimacy

- Status: Accepted (for a trusted group relay, `sender_auth` is the author's: amended by
  [0050](0050-group-relayed-mail-carries-its-authors-authentication.md))
- Date: 2026-10-14
- PR: #50

## Context

On 2026-10-14 a phishing email was classified `important` at 0.92 (spam 0.02) and flagged
`Jev/Needs Reply`. It came from a `noreply@` address on a news-like domain, sent through an ESP: SPF
and DKIM passed for the attacker's own domain, and Gmail's `Authentication-Results` had no DMARC
result. Its subject was "RE: <business subject>" with no `In-Reply-To` or `References`. The HTML was
a fake "Voice Letter ▶ 1 min 30 sec" whose play button went through an unrelated big brand's
click-tracking redirector, followed by a real thread between other people copied in (greeting two
other names, a government official's signature, a quoted From/Sent/To/Subject block) with links to
that government's domain.

Jev could not see most of this. `build_state` sent the headers' text, the list flags and the body
text, and body extraction drops every `href`, so Jev never saw where the links went, nor whether the
"reply" answered anything. The spam criterion ended with "bulk mail with forged or failing sender
authentication", which points the wrong way when authentication passes.

## Decision

Jev gets three more state keys, each only when it says something (as `previous_contact`):

- `reply_without_thread: true` when the subject starts with a reply or forward prefix in the
  languages clients localise it to, and the email has neither `In-Reply-To` nor `References`
  (`signals.reply_without_thread`).
- `link_domains`: the distinct registrable domains (Public Suffix List, ADR 0037) of the body's
  links, most frequent first, at most 8 (`body.extract_link_domains`). The sender's own domain is
  kept: links that match it are a sign of legitimacy Jev can weigh. Domains only, never a URL: paths
  and query strings carry recipients' addresses and tokens. URLs are read as a browser reads them
  (backslashes, one or no slash, scheme-relative and numeric hosts), and a host longer than DNS
  allows (253 characters, 63 per label) is dropped, so a link can't inflate Jev's state. On a
  private suffix (`github.io`, `workers.dev`) the domain keeps the site's own label, which can name
  a person or carry a per-recipient token: "never a token" holds for paths and queries, not there.
- `sender_auth`: the SPF, DKIM and DMARC verdicts from the same trusted `mx.google.com` header that
  `dmarc_pass` reads (ADR 0022), parsed per RFC 8601 (ADR 0043), `none` for a missing method; left
  out without that header.

The spam criterion says that passing SPF or DKIM only proves which domain sent the email, and names
the signs of phishing as mismatches: links to a brand unrelated to the sender, a reply subject with
no earlier message, a copied conversation between other people, made stronger by DMARC `none` or
`fail`. Voicemail, document and e-signature notices count as spam when they impersonate a service,
not for what they are: a notice from an established document, e-signature, tax-return or
client-portal service that passes DMARC and links to that service's own brand domains (which can
differ from the sending domain) is explicitly not spam, and `action_required` names reviewing or
signing through such a service (`transactional`: copies of documents already signed or filed).
`important` excludes a conversation between other people pasted in by an unrelated sender;
`needs_reply` says no to a conversation not addressed to me and to phishing. The category
instructions explain the three keys in the same terms: a pass only proves the sending domain,
`none`/`fail` strengthens other signs, and links to a service's own brand or portal domains are
normal.

No rule acts on the signals: Jev still decides, and the spam threshold (0.90) is unchanged.

## Alternatives considered

- A deterministic rule (e.g. spam when `reply_without_thread` and a link to an unrelated domain):
  Outlook forwards and some mobile clients omit thread headers, and ESP click tracking makes most
  bulk mail link to "unrelated" domains. Moving mail to Spam on that would misfire on real mail; Jev
  can weigh the signals with the content.
- Full URLs in the state: more for Jev to read, but they leak the recipient's address and one-time
  tokens into Jev's input, and the domain is what tells a lure apart.
- Only the sender-unrelated link domains: needs a notion of "related" (ESPs, CDNs, a company's other
  brands) that the code can't get right; Jev can.
- Naming lures by kind ("a lure to play, open or sign something", "links to an unrelated domain"):
  the first version of this PR did, and the real-mail check showed it pulling genuine tax-return
  notices (DMARC pass, links to the vendor's own portal and brand domains) from `action_required`
  toward spam (0.53 and 0.77). Phishing and real document services look alike in kind; they differ
  in whether the links, the reply and the authentication match the sender.
- Leaving out forward prefixes: the incident pattern also appears as "FW:"; the criteria present the
  flag as one sign among others, so a genuine forward is not condemned by it.

## Consequences

- Every classification sends a few more tokens (three short keys and about 60 more words of
  instructions and criteria).
- Phishing that dresses up as a reply, or hides its target behind another brand's redirector, is
  visible to Jev; whether it is caught still depends on Jev's answer reaching 0.90, below which it
  goes to Review instead of `important`.
- Genuine forwards without thread headers and newsletters with many link domains carry signals that
  look suspicious in isolation; the real-mail check before merging looks for those flipping to spam.
- A phishing email that passes DMARC on a lookalike domain and links only to that same domain fits
  none of the mismatch signs; Jev has to catch it from the content, as before.
