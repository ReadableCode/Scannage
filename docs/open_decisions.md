# Open decisions

Choices still to be made. Every entry says what the app does today, the
options, and a recommendation.

Delete an entry when it is decided and the code matches. Decisions already
made are in the git history of this file.

## Labels

### 1. Keeping track of what has been printed

Today: the label sheet prints whatever range is asked for, starting at tag 1
by default. Nothing records which tags have been printed. The boxes list
shows which tags are in use, which is not the same thing: a printed label
that is not on a box yet is in use nowhere.

Options:

1. Leave it. Keep the printed sheets together and read the last number off
   them.
2. Record a print when the print button is pressed, and start the sheet at
   the first tag that has never been printed. The label sheet would also mark
   each tag as printed, in use, or free.

Recommendation: option 2. The browser cannot tell whether paper came out, so
a print is recorded when the button is pressed and can be taken back.

### 2. What the label says

Today: a label reads `BOX 007` in large type next to the tag, and the small
QR code. Printing tag 7 again always gives the same label, so a reprint
replaces a damaged one exactly.

Options: leave it, or add the size of the set, as in `7 of 250`.

Recommendation: leave it. The number alone is what a reprint needs, and the
set size would be wrong on every label if the set ever grew.

## Running it yourself

### 3. Built in https for a new user

Today: the app can serve https itself with a certificate it generates. It is
off unless `SCANNAGE_HTTPS=1` is set, including in the standalone
`compose.yaml`.

Options: leave it off by default, or turn it on in the standalone
`compose.yaml` so a phone works on the first try.

Recommendation: leave it off. With it on, the browser shows a warning before
the first page, which is a poor first impression on a laptop.

### 4. The certificate follows the machine's address

Today: the built in https certificate names the machine's address on the
local network. When that address changes, the app makes a new certificate on
its next start and the phone asks again.

Options: leave it, or only name what is listed in `SCANNAGE_HTTPS_HOSTS` so
the certificate never changes on its own.

Recommendation: leave it, and give the machine a fixed address.

## The editor

### 5. Item rows on a phone

Today: each item row has a quiet `photo` control. It takes about 46 pixels,
so on a phone a long item name is cut short sooner than before: "Tent, 4
person" shows as "Tent, 4 p...".

Options: leave it, move the control into the photo viewer so rows get the
width back, or let a long name wrap onto a second line.

Recommendation: let the name wrap. Try it with real items first.
