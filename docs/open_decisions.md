# Open decisions

Choices the first version made so it could ship. Each one is easy to change
now and harder once real boxes are labelled. Every entry says what the app
does today, the options, and a recommendation.

Delete an entry when it is decided and the code matches.

## Decide before printing many labels

### 1. Tag capacity

Today: tags come from the `ARUCO_MIP_36h12` dictionary, which has 250
numbers. One number per box, so 250 boxes.

Options: keep it, or move to a larger dictionary. Larger dictionaries have
smaller cells, which shortens the distance a tag reads from.

Recommendation: keep 250. Changing the dictionary later means reprinting
every tag, so decide this first.

### 2. Label size

Today: the label sheet prints 6 per page or 2 per page. Read distance grows
with tag size and has only been tried on a screen, not on a shelf.

Options: one size for everything, or large labels for high shelves only.

Recommendation: print one page of each, tape them up, and walk back until
they stop reading before printing the rest.

### 3. What the QR code holds

Today: the small QR holds the full address of the box page. If the address
of the app ever changes, the QR codes stop working. The large tags do not
hold an address and keep working.

Options: accept a reprint if the address changes, or point the QR at a short
address that is promised never to change and redirects to the app.

Recommendation: accept it. The QR is a convenience and the live view does
not use it.

## Decide before real inventory goes in

### 4. Sample boxes

Today: with `SCANNAGE_SEED_SAMPLES=1` the app adds six sample boxes on tags
1 to 6 the first time it starts on an empty store, and never again. They
exist so there is something to look at on day one.

Options: delete them in the app and leave the setting on (it will not add
them back), or turn the setting off as well.

Recommendation: delete the six boxes in the app and remove the setting from
the deployment.

### 5. One shared inventory or one per person

Today: one inventory. Everyone who can reach the app sees and edits all of
it. The app records who made the last change when the sign-in proxy says
who the user is.

Options: keep it shared, or give each person or household its own boxes.

Recommendation: keep it shared. A garage is shared.

### 6. Who can sign in

Today: the app has no accounts. Whoever the sign-in proxy lets through is
in. Each person needs their own account at the proxy.

Options: proxy accounts only, or add accounts inside the app.

Recommendation: proxy accounts only.

### 7. How long a sign-in lasts

Today: the app inherits the proxy's session length. A short session means
signing in again at the shelf, with a phone in one hand.

Options: leave it, or give this app a longer session at the proxy.

Recommendation: a longer session for this app. It holds a list of what is in
some boxes.

## Decide when it starts to matter

### 8. The published port

Today: both compose files publish port 8791 on the host. Anyone on the same
network can reach the app on that port without going through the sign-in
proxy, and the app trusts a `Remote-User` header from whoever sends one.

Options: keep publishing it, stop publishing it and reach the app only
through the proxy, or make the app refuse requests that did not come from
the proxy.

Recommendation: stop publishing it once nothing depends on the direct port.

### 9. HTTPS for people running it on their own

Today: phone browsers only open the camera on an https page. A person who
runs the app at home needs their own https proxy in front of it. On the same
computer, `http://localhost:8791` works without one.

Options: leave it to the user, or let the app serve https itself with a
certificate it generates.

Recommendation: let the app serve https itself as an option. It is the
difference between a five minute setup and an afternoon.

### 10. Where tag detection runs

Today: detection runs on the page's main thread. It measured about 41 ms a
frame on one recent phone, which is smooth.

Options: leave it, or move detection to a worker so a slow phone stays
responsive.

Recommendation: leave it until a phone stutters.

### 11. Working without a connection

Today: the live view keeps showing the last contents it loaded if the
connection drops. Edits need a connection.

Options: leave it, or queue edits and send them when the connection returns.

Recommendation: leave it unless the garage wifi turns out to be the problem.

### 12. Photos and history

Today: a box has a name, a place, notes and items. No photos. Only the last
change is recorded, not a history.

Options: add item photos, add a change log, or neither.

Recommendation: neither until the inventory is in daily use.
