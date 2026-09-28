# Open decisions

Choices still to be made. Every entry says what the app does today, the
options, and a recommendation.

Delete an entry when it is decided and the code matches. Decisions already
made are in the git history of this file.

## Decide before printing many labels

### 1. Tag capacity

Today: tags come from the `ARUCO_MIP_36h12` dictionary, which has 250
numbers. One number per box, so 250 boxes.

What a larger dictionary costs, measured with the detector this app ships,
on a laptop, six real tags in view:

| Codes in the dictionary | Clean scene, 960 wide | Scene with 20 tag shaped squares that are not tags |
|---|---|---|
| 250 | 7 ms | 26 ms |
| 1000 | 12 ms | 73 ms |
| 4000 | 33 ms | 279 ms |

A phone is roughly four times slower than that laptop. The live view stays
smooth up to about 60 ms a frame.

- Speed: a tag that reads costs almost nothing more. The cost is in shapes
  that look like a tag and are not one: each is compared with every code in
  the dictionary. A tidy shelf barely notices. A cluttered one gets two to
  three times slower at 1000 codes.
- That cost comes from how the detector compares codes, as text, one
  character at a time. Comparing them as numbers would make the size of the
  dictionary nearly free. That is a change to vendored code.
- Storage: none that matters. A tag number is one integer in the database,
  and the dictionary itself is about 12 bytes a code in one JavaScript file.
- Read distance: unchanged as long as the tag keeps its 6 by 6 grid. A 7 by 7
  grid has cells 11% smaller and reads from 11% closer.
- Misreads: more codes in the same grid means codes that look more alike.
  The current dictionary keeps every code at least 12 cells different from
  every other.

Options:

1. Keep 250.
2. Move to a 6 by 6 dictionary with 587 codes. Same read distance, codes
   at least 11 cells apart, about twice the lookup cost in clutter.
3. Move to 1000 or more with a 7 by 7 grid, and rewrite the comparison so the
   size does not cost speed.

Recommendation: keep 250 unless more than 250 boxes is likely. If it is,
take option 2 now. Either way decide before printing, because changing the
dictionary means reprinting every tag.

## Decided, not applied yet

### 2. How long a sign-in lasts

Decided: a device signs in once and stays signed in. A stolen device is an
accepted risk.

Not applied: this is a setting of the sign-in proxy in front of the app, not
of the app. Two things are needed there:

- a session length of a year. Browsers cap a cookie at about 400 days, so a
  year is the most that can be promised.
- sessions that survive a restart of the proxy. A proxy that keeps sessions
  in memory signs everyone out each time it restarts.

## Decide when it starts to matter

### 3. Where photos are stored

Today: in the database, next to the inventory. A photo is scaled to 1600
pixels and stored as JPEG, roughly 200 KB, plus a small thumbnail.

Options: keep them in the database, or store files on disk and keep only the
path in the database.

Recommendation: keep them in the database. One thing to back up, and a
thousand photos is about 200 MB. Revisit if the database backup gets slow.

### 4. Photos of a deleted box

Today: deleting a box or an item deletes its photos. History keeps a line
saying how many photos went, not the photos.

Options: leave it, or keep photos of deleted things so history can show them.

Recommendation: leave it.

### 5. Built in https for a new user

Today: the app can serve https itself with a certificate it generates. It is
off unless `SCANNAGE_HTTPS=1` is set, including in the standalone
`compose.yaml`.

Options: leave it off by default, or turn it on in the standalone
`compose.yaml` so a phone works on the first try.

Recommendation: leave it off. With it on, the browser shows a warning before
the first page, which is a poor first impression on a laptop.

### 6. The certificate follows the machine's address

Today: the built in https certificate names the machine's address on the
local network. When that address changes, the app makes a new certificate on
its next start and the phone asks again.

Options: leave it, or only name what is listed in `SCANNAGE_HTTPS_HOSTS` so
the certificate never changes on its own.

Recommendation: leave it, and give the machine a fixed address.

### 7. Item rows on a phone

Today: each item row has a quiet `photo` control. It takes about 46 pixels,
so on a phone a long item name is cut short sooner than before: "Tent, 4
person" shows as "Tent, 4 p...".

Options: leave it, move the control into the photo viewer so rows get the
width back, or let a long name wrap onto a second line.

Recommendation: let the name wrap. Try it with real items first.
