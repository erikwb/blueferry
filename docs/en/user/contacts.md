# Contacts

BlueFerry keeps a local copy of the iPhone's address book (see
[Pairing](pairing.md) for **Sync Contacts**). Besides the names and addresses
used for messages, each contact card now keeps what the iPhone stores on it:
every phone number as written on the phone with its label (mobile, home,
work, iPhone, fax, pager, or your own label), labelled email addresses,
postal addresses, the company and the job title.

## Browse contacts

```bash
blueferry contacts list                 # first 50 cards, with their ids
blueferry contacts list --all           # every card
blueferry contacts list -q "example"    # filter by name, company, mail, address or number
blueferry contacts show 3f2a9c…         # every detail of one card
blueferry contacts show Alice           # works when exactly one card matches
blueferry contacts show Alice --photo alice.jpg   # also save the photo
```

A number filter matches the digits, so `-q "079 123"` finds `+41 79 123 45 67`.
Short digit strings (fewer than three digits) are not treated as numbers.

The card ids stay the same while a card is unchanged, until the background
service restarts. After a sync that changed a card, its old id no longer
works; list the contacts again.

Contact view in the clients:

- **KDE client:**
  not yet; use the command line.
- **GTK client:**
  the **Contacts** tab lists the cards with a search field (it searches as
  you type, after a short pause) and loads more cards when you scroll to the
  end. Choose a card to see all its details; **Call** asks before it dials
  the first number, **Message** opens the conversation with it or starts a
  new one, and each number or address has its own call, message, email and
  copy buttons. The list is read only while the tab is shown, and reloads by
  itself when the background service restarts or the contacts were synced
  again.
- **Terminal client:**
  not yet; use the command line.
- **Quickshell panel:**
  not yet; use the command line.

## Photos

A card's photo is shown only with [contact photos](contact-photos.md)
turned on. The list itself never transfers photos; `contacts show` fetches
the one photo of the card you open.

## Storage and privacy

- The extra card details are stored only inside the encrypted contact
  records, under the same [storage mode](privacy.md#local-data) as the rest
  of the contact cache. They are never written in readable form.
- With the unencrypted storage mode, BlueFerry keeps only names, numbers and
  email addresses as before: labels, postal addresses, company and job
  title are not kept, and cards show the stored numbers without labels.
- Contacts synced by an earlier version show their numbers without labels
  until the next contact sync.
- Notes, birthdays, websites and other card fields are not kept.

## Limits

- Tested with sample vCards only, not yet against a real iPhone's
  address book.
