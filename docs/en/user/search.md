# Search

One search looks through your conversations, messages, contacts and the
call history at once.

```bash
blueferry search lunch          # every group, up to 10 hits each
blueferry search "079 123" -n 30
blueferry search alice --ids    # also print the ids to open each hit
```

Results are grouped into conversations (by name or group member),
messages (by text, newest first, with the matching words highlighted),
contacts (name, company, mail, postal address or number) and calls (caller
name or number). Each group shows how many matches the limit left out.
Case, accents and spelling variants do not matter: `muller` finds
`Müller`. Numbers match by their digits once the query has at least three
digits, also for conversations. The background service returns at most
50 hits per group (`-n 50`). Queries shorter than two characters find
nothing.

Search in the clients:

- **KDE client:**
  not yet; use the command line.
- **GTK client:**
  the magnifier in the header bar, `Ctrl+K` or `Ctrl+F` opens the search. It
  searches as you type, after a short pause, and shows the same groups with
  up to 20 hits each; Enter opens the first hit, a click or the arrow keys
  any other: the conversation scrolled to the message, the contact card, or
  the call in the call history. Closing the search forgets the results.
- **Terminal client:**
  not yet; use the command line.
- **Quickshell panel:**
  not yet; use the command line.

## What is searched

- Messages: the conversations BlueFerry holds for its clients, that is the
  most recent 2,000 messages plus starred conversations. Older messages
  outside starred conversations are not searched.
- Contacts: the contact cache, including the [card details](contacts.md).
- Calls: the retained [call history](call-history.md), only while it is
  turned on.

## Privacy

Local history is encrypted, so BlueFerry keeps no search index on disk.
The background service searches the data it already holds decrypted in
memory for the open clients, and only when you search. To keep searching
while you type fast, it keeps a lower-case copy of the searched messages in
memory until the conversations change; nothing is prepared in advance or
written to disk. While the wallet is locked, calls are not
searched.

## Limits

- Tested with sample data only, not yet against a real iPhone's history.
