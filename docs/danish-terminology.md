# Danish terminology (binding)

The single source of truth for the Danish words used in `i18n/strings.csv`. If a concept is in this
table, every row uses the term below and no other. If a term is missing, add it here in the same commit
that adds the first row needing it — that is how the glossary stays complete.

The English column is the canonical English wording; the Danish column is what the UI shows. Column
headers are listed separately where the Danish differs from the prose form (for example a money column
header states the currency).

## Money and numbers

| English | Danish |
|---|---|
| price | pris |
| price (money column header) | Pris (DKK) — row `common.price_dkk` |
| total / sum | i alt |
| subtotal | subtotal |
| quantity / qty | antal |
| stock | lager (På lager / Ikke på lager / Lavt lager) |
| amount | beløb |
| average / mean | gennemsnit |
| median | median |
| minimum / maximum | mindste / største |
| saved / spent | sparet / brugt |
| exchange/currency code | DKK (never `kr.`, never a symbol) |

## Navigation and screens

| English | Danish |
|---|---|
| Catalogue | Katalog |
| Purchase list | Indkøbsliste |
| Purchase history | Købshistorik |
| Profile | Profil |
| Account | Konto |
| Settings | Indstillinger |
| Table / table view | Tabel / Tabelvisning |
| Analysis | Analyse |
| Admin | Administration |
| Members | Medlemmer |
| Scan requests | Scananmodninger |
| Log in / Log out | Log ind / Log ud |
| Register (verb) | Opret konto |
| Back | Tilbage |
| Close | Luk |
| Done | Færdig |
| Cancel | Annuller |
| Save | Gem |
| Delete | Slet |
| Edit | Redigér |
| Add | Tilføj |
| Search | Søg |
| Filter | Filter |
| Sort | Sortér |
| Clear | Ryd |
| Export | Eksportér |
| Print | Udskriv |
| Refresh | Opdatér |
| Yes / No | Ja / Nej |
| My profile | Min profil |
| Account → Security | Konto → Sikkerhed |
| Security (tab) | Sikkerhed |
| role | rolle |

## People and roles

| English | Danish |
|---|---|
| member | medlem |
| staff (role) / staff (people, in a sentence) | personale / personalet |
| admin / administrator | administrator |
| user | bruger |
| customer | kunde |
| Welcome | Velkommen |
| Hello / Hi, {name} | Hej {name} |

## Items and scanning

| English | Danish |
|---|---|
| item / items | vare / varer |
| item name | varenavn |
| catalogue item | katalogvare |
| add to catalogue | tilføj til kataloget |
| unknown item | ukendt vare |
| suggestion | forslag |
| category | kategori |
| description | beskrivelse |
| name | navn |
| image / image URL | billede / billed-URL |
| similar items | lignende varer |
| barcode | stregkode |
| QR badge / badge | QR-badge / badge |
| scanner | scanner |
| scan (verb) / scanning (noun) | scan / scanning (plural: scanninger) |
| paired / unpaired | parret / ikke parret |
| pair (verb) / pairing / unpair | parre / parring / fjern parring |
| pairing file | parringsfil |
| live / off | aktiv / inaktiv |
| grab daemon | grab-daemon |
| status pill | statusknap |
| scan request | scananmodning |
| badge check | badgekontrol |
| revoke (a badge) | tilbagekald (tilbagekaldt) |
| restricted circulation | begrænset cirkulation |

## Forms and account

| English | Danish |
|---|---|
| email | e-mail |
| password | adgangskode |
| current password | nuværende adgangskode |
| new password | ny adgangskode |
| confirm password | gentag adgangskode |
| temporary password | midlertidig adgangskode |
| change password | skift adgangskode |
| reset password | nulstil adgangskode |
| set password | angiv adgangskode |
| save changes | gem ændringer |
| phone | telefon |
| username | brugernavn |
| session | session |
| log off / logoff (noun) | log ud / udlogning |
| switch (account) | skift |
| Discard | Kassér |
| required | påkrævet |
| invalid | ugyldig |
| too short | for kort |
| mismatch | stemmer ikke overens |
| full name | Fuldt navn |

## Catalogue, cart and checkout

| English | Danish |
|---|---|
| cart / purchase list | indkøbsliste |
| add to purchase list | Tilføj til indkøbslisten |
| clear list | ryd listen |
| checkout | kasse |
| review purchase | gennemse køb |
| complete purchase | gennemfør køb |
| purchase (noun) / purchase (day count) | køb (invariant plural: 1 køb / 3 køb) |
| receipt | kvittering |
| order | ordre |
| line | linje |
| each | pr. stk. |
| price each | {price} pr. stk. |
| skipped (over) | sprunget over |
| clamped quantity | {applied} af {requested} |
| in stock / out of stock | på lager / ikke på lager |
| keep shopping | fortsæt med at handle |
| relevance / semantic search | relevans / semantisk søgning |
| embedding | embedding |
| meaning-similarity | betydningslighed |
| similar meaning | lignende betydning |
| same barcode family | samme stregkodefamilie |
| restored (a list) / imported | gendannet / importeret |
| saved (a list) | gemt (not `sparet`, which is the money sense) |
| autosaved while you edit | gemmes med det samme |

## Purchases, calendar and dates

| English | Danish |
|---|---|
| calendar | kalender |
| spent in {month} | brugt i {month} |
| this month / spent this month | denne måned / brugt denne måned |
| today | i dag |
| load more | indlæs flere |
| new (chip) | ny |
| requested (quantity) | bestilt |
| purchase history lock hint | …låse din købshistorik op |
| weekday labels (grid) | ma ti on to fr lø sø — CSV `date.dow.*` |
| month label (grid) | jan feb mar apr maj jun jul aug sep okt nov dec — CSV `date.month.*` |
| month name in a sentence | januar … december — CSV `date.month_long.*` |

A grid short must never be reused inside a sentence: `Brugt i jan` is wrong, `Brugt i januar` is right.

## Mechanical rules

- **Sentence case**, never Title Case. Lowercase after the first word unless the word is a proper noun
  (Katalog, Indkøbsliste, Købshistorik, Profil, Konto, Indstillinger, Tabelvisning, Analyse, Administration,
  Medlemmer, Scananmodninger, Eksportér, Søg, Gem, Slet, Annuller, Luk, Færdig, Tilføj, Opdatér, Udskriv,
  Ja, Nej, Velkommen, Hej).
- **Buttons are verb-first imperatives**: `Gem`, `Slet`, `Eksportér`, `Opdatér`.
- **`du`-form**, never `De`.
- **Placeholders** (`{name}`) are byte-identical to the English row; the validator enforces the multiset,
  including repeat counts.
- **Plurals** are `.one` / `.other` row pairs. Never `(s)`, never `vare(r)`.
- **No `!`** unless the English row has one. **No emoji** anywhere in the copy source.
- **Machine values are never translated**: `member|staff|admin`, `live|off|unpaired`, route hashes, storage
  keys (`hcrm_*`), API paths, SKUs, status codes and `data-*` values. Only their visible labels are copy.
- Money renders as `1.234,50 DKK` in both UI languages; dates follow the UI locale (`da-DK` → `5.1.2026`,
  `en-GB` → `05/01/2026`).

## Relationship to the older localization spec

`docs/spec-da-i18n.md` (2026-10-20) proposed a single-language translation with no message catalogue and
several different terms. The owner's later directive (2026-11-01) replaced those decisions; where the two
disagree, **this glossary and the shipped implementation win**:

| Spec said | Shipped instead | Why |
|---|---|---|
| No language switcher; Danish replaces English | Danish by default with a flag that switches to English | owner requirement 4 |
| Translate literals in place, no catalogue | one CSV of strings compiled into the message bundle the app loads | owner requirements 2 and 3 |
| Currency `kr. / DKK` | `DKK` only | owner requirement 1 |
| Cart = `Kurv` | cart = `Indkøbsliste` | the app's own concept is "purchase list"; one term everywhere |
| Brand rename HCRM → `Lageret` | brand stays the language-neutral `HCRM` | not requested by the owner; the title must not be language-specific |
| Seed catalogue translated to Danish | user-supplied content stays as entered | item names are data, not UI copy |
