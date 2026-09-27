"""Step 2: Redact. Replace client details with placeholders in the copy sent to the LLM.

Never changes a dollar amount, percent, limit, non-birth date, or zip code.

    python -m app.redact fixtures/X.pdf --name "Jane Doe" --address "1 Main St, Town, NC 28000" \
        --other "John Doe, Kid Doe"
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field

from app.config import lines_config, settings

# Common words that must never be treated as a name word, on top of every label in lines.yaml.
COMMON_WORDS = """
law home auto price cost hall rich street coverage coverages premium premiums total policy
insurance insured company liability property damage dwelling vehicle vehicles driver drivers
rental water fire flood wind hail storm mutual liberty travelers progressive national general
heritage steadily safeco orion select payment payments discount discounts quote quotes date
name agent agency deductible limit limits included excluded rejected basic towing medical
personal other structures contents loss assessment service line equipment breakdown ordinance
replacement actual cash value umbrella landlord motorcycle fair rent rents premises theft
animal mold business credit card identity inflation guard hurricane named account amount
annual monthly state county city north south east west main park lake hill river bank
road drive lane court place avenue ave boulevard highway center first second third
mailing address phone email fax page print printed issue effective expiration period term
full plan fees fee taxes estimate estimated summary optional additional increased special
standard preferred select essential choice protect protection security secure safe
""".split()

NAME_WORD_MIN = 4


@dataclass
class Client:
    name: str = ""
    address: str = ""
    other_names: list[str] = field(default_factory=list)


@dataclass
class Span:
    start: int
    end: int
    placeholder: str
    kind: str

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


def stoplist() -> set[str]:
    cfg = lines_config()
    words = set(COMMON_WORDS)
    for line in cfg["lines"].values():
        texts = [line.get("label", "")]
        for group in ("core", "extras"):
            texts += [v.get("label", "") for v in (line.get(group) or {}).values()]
        for t in texts:
            words.update(w.lower() for w in re.findall(r"[A-Za-z]+", t))
    return words


def _flex(s: str) -> str:
    """Regex for a literal string with any whitespace run matching any whitespace run."""
    return r"\s+".join(re.escape(p) for p in s.split())


_ACCENTS = {"a": "aàáâãäå", "c": "cç", "e": "eèéêë", "i": "iìíîï", "n": "nñ", "o": "oòóôõöø", "u": "uùúûü", "y": "yýÿ"}


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _name_word(w: str) -> str:
    """Regex for one typed name word as a document may print it: with or without accents
    ("José" / "JOSE"), any apostrophe or none ("O'Brien" / "O’Brien" / "OBRIEN"), and a hyphen
    or a space ("O'Brien-Hart" / "OBRIEN HART")."""
    out = []
    for ch in _fold(w) or w:
        if ch in "'’`":
            out.append("['’`]?")
        elif ch == "-":
            out.append(r"(?:-|\s+)")
        elif ch.lower() in _ACCENTS:
            out.append(f"[{_ACCENTS[ch.lower()]}]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _name_pattern(words: list[str]) -> str:
    """The words in order; a middle initial may stand between them ("Ann M Lee" for "Ann Lee")."""
    return r"\s+(?:[a-z]\.?\s+)?".join(_name_word(w) for w in words)


# ---------------------------------------------------------------- patterns

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
# OCR sometimes reads the last dot of an email as a comma.
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:[.,][\w-]+)*[.,][A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<![\w$.,/-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?![\w/-]|[.,]\d)")
LICENSE_MASKED_RE = re.compile(r"\b[X*]{4,}[A-Z0-9]{2,6}\b")
LICENSE_LABELED_RE = re.compile(
    r"(?i)\b(?:driver'?s?\s+license|license\s*(?:no\.?|number|#))\s*[:#]?\s*"
    r"(?-i:(?=[A-Z0-9-]*\d)([A-Z0-9-]{5,20}))\b")
SSN_RE = re.compile(r"(?<![\w-])(?:\d{3}|[X*]{3})-(?:\d{2}|[X*]{2})-\d{4}(?![\w-])")
SSN_LABELED_RE = re.compile(r"(?i)\b(?:SSN|social\s+security(?:\s+(?:no\.?|number|#))?)\s*[:#]?\s*(\d{9})\b")
DATE = r"\d{1,2}[/-][\d*]{1,2}[/-]\d{2,4}"
DOB_LABELED_RE = re.compile(rf"(?i)\b(?:date\s+of\s+birth|DOB|birth\s*date)\b\s*[:#]?\s*({DATE})")
DOB_HEADER_RE = re.compile(r"(?i)\b(?:date\s+of\s+birth|DOB|birth\s*date)\b")
DATE_RE = re.compile(rf"(?<![\d/]){DATE}(?![\d/])")
MASKED_DATE_RE = re.compile(r"(?<![\d/])\d{1,2}/\*{1,2}/\d{4}(?![\d/])")


def _find_people(text: str, client: Client, stop: set[str]) -> list[Span]:
    spans = []
    people = [(client.name, "[CLIENT_NAME]")] if client.name.strip() else []
    people += [(n, f"[PERSON_{i}]") for i, n in enumerate(
        [n for n in client.other_names if n.strip()], start=1)]
    # full strings first (case-insensitive), then "Last, First" order
    for full, ph in people:
        words = full.split()
        variants = [words] + ([[words[-1] + ","] + words[:-1]] if len(words) > 1 else [])
        for v in variants:
            for m in re.finditer(rf"(?i)(?<!\w){_name_pattern(v)}(?!\w)", text):
                spans.append(Span(m.start(), m.end(), ph, "name"))
    # then each name word alone: 4+ letters, capitalized in the text, not an insurance word
    for full, ph in people:
        for w in re.findall(r"[^\W\d_][\w'’-]+", full):
            parts = [w] + (w.split("-") if "-" in w else [])  # "O'Brien-Hart": also "O'Brien", "Hart"
            for part in parts:
                bare = re.sub(r"['’`]", "", _fold(part))
                if len(bare) < NAME_WORD_MIN or bare.lower() in stop:
                    continue
                for m in re.finditer(rf"(?i)(?<![\w-]){_name_word(part)}(?![\w-])", text):
                    if m.group(0)[0].isupper():
                        spans.append(Span(m.start(), m.end(), ph, "name_word"))
    return spans


def _find_address(text: str, address: str) -> list[Span]:
    if not address.strip():
        return []
    spans = [Span(m.start(), m.end(), "[CLIENT_ADDRESS]", "address")
             for m in re.finditer(rf"(?i)(?<!\w){_flex(address)}(?!\w)", text)]
    # The street line on its own: house number and street name; the suffix word (Lane, Ln,
    # Dr, or an OCR misread of it) may differ from what was typed.
    street = address.split(",")[0].split()
    if len(street) >= 2 and street[0][0].isdigit():
        core = street[:-1] if len(street) >= 3 else street
        pat = rf"(?i)(?<![\w-]){_flex(' '.join(core))}(?:\s+[A-Za-z]{{1,12}}\.?)?(?!\w)"
        spans += [Span(m.start(), m.end(), "[CLIENT_ADDRESS]", "address")
                  for m in re.finditer(pat, text)]
    return spans


def _find_patterns(text: str) -> list[Span]:
    spans = []
    for m in EMAIL_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "[EMAIL]", "email"))
    for m in VIN_RE.finditer(text):
        if re.search(r"\d", m.group(0)) and re.search(r"[A-Z]", m.group(0)):
            spans.append(Span(m.start(), m.end(), "[VIN]", "vin"))
    for m in LICENSE_MASKED_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "[LICENSE]", "license"))
    for m in LICENSE_LABELED_RE.finditer(text):
        spans.append(Span(m.start(1), m.end(1), "[LICENSE]", "license"))
    for m in SSN_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "[SSN]", "ssn"))
    for m in SSN_LABELED_RE.finditer(text):
        spans.append(Span(m.start(1), m.end(1), "[SSN]", "ssn"))
    for m in DOB_LABELED_RE.finditer(text):
        spans.append(Span(m.start(1), m.end(1), "[DOB]", "dob"))
    for m in MASKED_DATE_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "[DOB]", "dob"))
    # a DOB column header with the dates in the rows below it: birth years only
    lines = text.split("\n")
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln) + 1)
    for i, ln in enumerate(lines):
        if DOB_HEADER_RE.search(ln) and not DOB_LABELED_RE.search(ln):
            for j in range(i + 1, min(i + 6, len(lines))):
                for m in DATE_RE.finditer(lines[j]):
                    year = int(m.group(0)[-4:]) if len(m.group(0).split("/")[-1]) == 4 else None
                    if year and year <= 2015:
                        spans.append(Span(offsets[j] + m.start(), offsets[j] + m.end(), "[DOB]", "dob"))
    for m in PHONE_RE.finditer(text):
        spans.append(Span(m.start(), m.end(), "[PHONE]", "phone"))
    return spans


# ---------------------------------------------------------------- names found next to labels

# A label that introduces a person's name ("Named Insured", "Prepared for", ...). Catches names
# even when the broker typed the client's name differently or not at all. The names it finds are
# added to the people list, so every other mention in the document is hidden too.
NAME_LABEL = (r"(?:primary\s+|proposed\s+)?(?:named\s+insureds?|insured\s+names?|insureds?|policy\s*holders?|prepared\s+for|"
              r"applicants?(?:\s+(?:name|information))?|driver\s+names?|your\s+driver\(s\)|customer\s+name|client\s+name)")
LABEL_ONLY_RE = re.compile(rf"(?i)^{NAME_LABEL}\s*:?$")
LABEL_VALUE_RE = re.compile(rf"(?i)^{NAME_LABEL}\s*:\s*(\S.*)$")
NAME_TOKEN_RE = re.compile(r"^(?:[A-Z][a-z]+(?:['-][A-Za-z]+)*|[A-Z]{2,}(?:['-][A-Z]+)*|[A-Z]\.?)$")
NOT_NAME_WORDS = {  # company and column-header words: a cell with one of these is not a person
    "bank", "mortgage", "insurance", "agency", "company", "inc", "llc", "corp", "corporation", "services",
    "credit", "union", "financial", "trust", "group", "associates", "fsb", "lender", "number", "type",
    "status", "information", "details", "your", "our", "the", "of", "for", "to", "by", "with", "none",
    "unknown", "yes", "no", "n/a", "same", "above", "below", "applicant", "insured", "named", "age", "male",
    "female", "dob", "sex", "gender", "state", "tobacco", "non-tobacco", "smoker", "height", "weight"}
ALSO_SURNAMES = {"law", "hall", "rich", "park", "lake", "hill", "river", "page", "price", "west", "north",
                 "south", "east", "lane", "court", "king", "young", "mark", "guard", "fair"}


def _name_like(cell: str, stop: set[str]) -> list[str]:
    """The person names in a table cell ('JOHN & JANE SMITH' -> both), or [] if it is not names."""
    cell = cell.strip().rstrip(",;")
    if not cell or len(cell) > 60 or re.search(r"[\d@$%#/\\()\[\]]", cell):
        return []
    parts = [x.strip(" ,") for x in re.split(r"\s+(?:&|and|AND)\s+", cell)]
    names = []
    for part in parts:
        tokens = part.replace(",", " ").split()
        if not 2 <= len(tokens) <= 4 and not (len(parts) > 1 and len(tokens) == 1):
            return []
        for t in tokens:
            low = t.lower().strip(".")
            if not NAME_TOKEN_RE.match(t) or low in NOT_NAME_WORDS or (low in stop and low not in ALSO_SURNAMES):
                return []
        if sum(len(t.strip(".")) >= 2 for t in tokens) < 1:
            return []
        if len(tokens) >= 2:
            names.append(" ".join(tokens) if "," not in part else part)
    if len(parts) > 1:  # 'JOHN & JANE SMITH': the full cell too, and the shared last name on each first name
        last = parts[-1].split()[-1]
        names += [f"{p} {last}" for p in parts[:-1] if len(p.split()) == 1] + [cell]
    return names


def _name_prefix(text: str, stop: set[str]) -> list[str]:
    """The name at the start of a label's value: 'RAFAEL QUINTERO-BASS Age 45' -> the name only
    (a text layer often prints the next column on the same line with a single space)."""
    tokens = text.split()
    for n in range(min(len(tokens), 8), 1, -1):
        names = _name_like(" ".join(tokens[:n]), stop)
        if names:
            return names
    return []


def find_labeled_names(texts: list[str], stop: set[str] | None = None) -> list[str]:
    """Names printed next to a name label: after 'Label: ' on the same line, or below a label
    cell in the same column (up to 3 rows: 'Prepared for:' / 'JANE DOE' / 'JOHN DOE')."""
    stop = stop if stop is not None else stoplist()
    found: list[str] = []
    for text in texts:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            cells = [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]
            for k, (col, cell) in enumerate(cells):
                m = LABEL_VALUE_RE.match(cell)
                if m:
                    found += _name_prefix(m.group(1), stop)
                    continue
                if not LABEL_ONLY_RE.match(cell):
                    continue
                if cell.endswith(":") and k + 1 < len(cells):  # 'Named Insured:    JANE DOE'
                    found += _name_like(cells[k + 1][1], stop)
                for below in lines[i + 1:i + 4]:
                    cell_below = next((c for c0, c in ((m2.start(), m2.group()) for m2 in re.finditer(
                        r"\S+(?: \S+)*", below)) if abs(c0 - col) <= 3), None)
                    names = _name_like(cell_below, stop) if cell_below else []
                    if not names:
                        break
                    found += names
    return list(dict.fromkeys(found))


def with_labeled_names(pages: list[dict], client: Client, stop: set[str]) -> Client:
    known = {" ".join(n.lower().split()) for n in [client.name, *client.other_names] if n.strip()}
    extra = [n for n in find_labeled_names([p["text"] for p in pages], stop)
             if " ".join(n.lower().split()) not in known]
    if not extra:
        return client
    return Client(client.name, client.address, [*client.other_names, *extra])


PRIORITY = {"name": 0, "address": 1, "email": 2, "vin": 3, "ssn": 4, "license": 4, "dob": 5, "phone": 6, "name_word": 7}


def find_spans(text: str, client: Client, stop: set[str] | None = None) -> list[Span]:
    stop = stop if stop is not None else stoplist()
    candidates = _find_people(text, client, stop) + _find_address(text, client.address) + _find_patterns(text)
    candidates.sort(key=lambda s: (PRIORITY[s.kind], -(s.end - s.start), s.start))
    chosen: list[Span] = []
    for s in candidates:
        if not any(s.overlaps(c) for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def apply_spans(text: str, spans: list[Span]) -> str:
    out, pos = [], 0
    for s in spans:
        out.append(text[pos:s.start])
        out.append(s.placeholder)
        pos = s.end
    out.append(text[pos:])
    return "".join(out)


def redact_pages(pages: list[dict], client: Client) -> tuple[list[dict], int]:
    """Returns (pages with redacted `text` and `replacements`, total replacements).

    Each replacement keeps the original string, for the local report only.
    """
    if not settings()["redaction"]["enabled"]:
        return [{**p, "replacements": []} for p in pages], 0
    stop = stoplist()
    client = with_labeled_names(pages, client, stop)
    out, total = [], 0
    for p in pages:
        spans = find_spans(p["text"], client, stop)
        total += len(spans)
        out.append({**p, "original_text": p["text"], "text": apply_spans(p["text"], spans),
                    "replacements": [{"start": s.start, "end": s.end, "original": p["text"][s.start:s.end],
                                      "placeholder": s.placeholder, "kind": s.kind} for s in spans]})
    return out, total


# ---------------------------------------------------------------- money check

MONEY_RE = re.compile(
    r"\(?-?\$\s?\d[\d,]*(?:\.\d+)?\)?"          # $1,447,000.00  ($145.00)  $ 308.00
    r"|-?\d[\d,]*(?:\.\d+)?\s?%"                 # 1%  10 %
    r"|(?<![\w.-])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\w)"  # 250,000/500,000  1,500
    r"|(?<![\w/.,-])-?\d+\.\d{2}(?![\w.])"       # 5.00  -12.00
)


def money_strings(text: str) -> list[str]:
    return MONEY_RE.findall(text)


def main(argv: list[str] | None = None) -> None:
    from app.read import read_file
    ap = argparse.ArgumentParser(description="Redact a quote file's text.")
    ap.add_argument("file")
    ap.add_argument("--name", default="")
    ap.add_argument("--address", default="")
    ap.add_argument("--other", default="", help="comma-separated other household names")
    args = ap.parse_args(argv)
    client = Client(args.name, args.address, [n.strip() for n in args.other.split(",") if n.strip()])
    pages, n = redact_pages(read_file(args.file), client)
    for p in pages:
        print(f"<page n=\"{p['page']}\" source=\"{p['source']}\">\n{p['text']}\n</page>")
    print(f"{n} items hidden", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
