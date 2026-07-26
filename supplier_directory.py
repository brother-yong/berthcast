"""Duplicate detection over supplier names.

Pure functions only -- no SQL, no Flask. All database work for the supplier
directory lives in database.py, per the repo convention.

Two passes, because neither catches the other's cases. Measured on real-shaped
names:

    nordvik / nordvic              difflib 0.857   fuzzy only
    brookvale / brokvale           difflib 0.941   fuzzy only
    padimastrading / padimas       difflib 0.667   suffix-strip only

Pass 1 also groups companies that merely share a first word (PADIMAS TRADING vs
PADIMAS ENTERPRISE both strip to "padimas"). That false positive is accepted on
purpose: it is the price of catching NORDVIK / NORDVIK TRADING, and it is why a
merge is only ever SUGGESTED and never applied without the user confirming.
"""
import difflib
import re

from agents.shared import normalise_match_key

# Legal-form and generic trade words that distinguish nothing between two
# spellings of the same trader. Kept deliberately short: every word added here
# widens the net for false groups as well as real ones.
_SUFFIX_WORDS = {
    "pte", "ltd", "llp", "co", "sdn", "bhd",
    "trading", "enterprise", "enterprises",
    "import", "export", "imports", "exports",
    "intl", "international", "sons", "and",
}

_WORD_RE = re.compile(r"[^a-z0-9]+")

# 0.85 catches single-character typos (0.857-0.941 measured) while leaving
# same-first-word companies apart (0.645-0.667 measured).
_FUZZY_THRESHOLD = 0.85


def _words(name):
    return [w for w in _WORD_RE.split(str(name).casefold()) if w]


def _stripped_key(name):
    """Normalised key with legal/trade words removed.

    Falls back to the full key when a name is nothing BUT suffix words, so
    e.g. a supplier literally called "Trading Co" still keys to something.
    """
    kept = [w for w in _words(name) if w not in _SUFFIX_WORDS]
    return "".join(kept) or normalise_match_key(name)


def _removed_words(name):
    return [w for w in _words(name) if w in _SUFFIX_WORDS]


def _suffix_groups(names):
    """{stripped_key: [names]} for keys shared by 2+ distinct names."""
    buckets = {}
    for n in names:
        buckets.setdefault(_stripped_key(n), []).append(n)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def _fuzzy_pairs(keys):
    """[(key_a, key_b)] for normalised keys similar enough to be a typo.

    quick_ratio() is an upper bound on ratio() and much cheaper, so it screens
    out the overwhelming majority of pairs before the real comparison.
    """
    keys = sorted(set(keys))
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            sm = difflib.SequenceMatcher(None, a, b)
            if sm.quick_ratio() < _FUZZY_THRESHOLD:
                continue
            if sm.ratio() >= _FUZZY_THRESHOLD:
                out.append((a, b))
    return out


def detect_duplicate_groups(names):
    """Return [{"group_key": str, "members": {name: reason}}].

    Groups from both passes are unioned: any two groups sharing a member become
    one group, so a set of spellings caught by both passes is offered once.
    """
    names = [n for n in dict.fromkeys(n for n in names if str(n).strip())]
    if len(names) < 2:
        return []

    # parent[name] -> representative name (plain union-find, no ranking; the
    # lists here are hundreds of names, not millions)
    parent = {n: n for n in names}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    reasons = {}

    # Pass 1 — suffix strip
    for _key, members in _suffix_groups(names).items():
        dropped = sorted({w for m in members for w in _removed_words(m)})
        reason = ("same name after removing " + ", ".join(w.upper() for w in dropped)
                  if dropped else "same name ignoring spacing and punctuation")
        for m in members:
            reasons.setdefault(m, reason)
            _union(members[0], m)

    # Pass 2 — fuzzy
    by_key = {}
    for n in names:
        by_key.setdefault(normalise_match_key(n), []).append(n)
    for key_a, key_b in _fuzzy_pairs(list(by_key)):
        for a in by_key[key_a]:
            for b in by_key[key_b]:
                reasons.setdefault(a, "spelled almost the same")
                reasons.setdefault(b, "spelled almost the same")
                _union(a, b)

    clusters = {}
    for n in names:
        if n in reasons:
            clusters.setdefault(_find(n), []).append(n)

    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        groups.append({
            # Keyed off the alphabetically-first member so the key is stable
            # across rebuilds and input orderings -- the ignore list depends on
            # that stability to keep a dismissed group dismissed.
            "group_key": normalise_match_key(members[0]),
            "members": {m: reasons[m] for m in members},
        })
    return sorted(groups, key=lambda g: g["group_key"])
