"""Comparing two Office packages part by part, without crying wolf.

A .docx is a zip of XML parts, and the fill engine is only ever allowed to
rewrite one of them. Proving that means comparing every other part of the
rendered file against the template it came from -- which sounds like `cmp` and
is not, because saving a package re-serialises all of it. Attribute order moves,
the encoding declaration changes, whitespace between elements is rewritten. Byte
comparison reports a difference in every part of every document and is therefore
worth nothing.

So this module compares parts *semantically*, and the exceptions below are the
list of ways a naive canonicalisation still produced false alarms on real
customer templates. Every one of them was a gate that had to be believed:

  * `[Content_Types].xml` and the `.rels` parts are unordered maps -- one
    entry per part, keyed by name. python-docx writes them back sorted, so
    comparing them in document order reports a difference where nothing
    changed. They are compared as sets.
  * `<Default>` in `[Content_Types].xml` declares a content type per file
    extension, and the writer emits only the extensions the package actually
    uses. A template that once held a .gif keeps the declaration; a letter
    rendered from it does not. Only declarations some part relies on are
    compared.
  * A `.rels` part with no `<Relationship>` children references nothing, and
    python-docx drops it on save rather than writing an empty map back. That is
    normalisation, not loss.
  * XML c14n refuses some real-world parts outright -- SharePoint ships
    `customXml/item*.xml` with binding prefixes as `ct:_=""`. Raw bytes are the
    fallback there, which is strict enough because nothing rewrites them.

What it does not do is compare rendered geometry: this is a structural diff of
the package, not a visual diff of the pages. §15 lists visual/layout diff QA as
async work, and it needs a renderer that produces pixels.
"""

import zipfile
from dataclasses import dataclass, field

from lxml import etree

#: Parts that are compared as unordered sets of children rather than in order.
_UNORDERED_PARTS = ("[Content_Types].xml",)

#: How many differences one diff reports before it stops looking. A package that
#: differs in fifty parts has one cause, and enumerating all fifty buries it.
DEFAULT_MAX_DIFFERENCES = 6


@dataclass
class PackageDiff:
    """What separates two Office packages, as far as the diff looked."""

    dropped: list = field(default_factory=list)
    added: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    #: (part name, parser message) for parts the caller required to still parse.
    malformed: list = field(default_factory=list)
    #: Set when a package could not be opened or a required part was absent.
    #: Everything else on the diff is then whatever was learned before that.
    unreadable: str | None = None
    #: The changed-part scan hit `max_differences` and stopped. Reported on the
    #: object rather than as a difference of its own: callers turn differences
    #: into reviewer-facing notes, and "there may be more" is not a defect.
    truncated: bool = False

    @property
    def identical(self) -> bool:
        return not (self.dropped or self.added or self.changed or self.malformed or self.unreadable)


def canonical_part(name: str, raw: bytes, package_names) -> object:
    """A comparable form of one part: semantic where possible, bytes where not.

    `package_names` is the name list of the package this part came from, which
    `[Content_Types].xml` needs to know which extension declarations are live.
    """
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return raw
    try:
        if name in _UNORDERED_PARTS:
            used = {n.rsplit(".", 1)[-1].lower() for n in package_names if "." in n}
            keep = []
            for child in root:
                tag = etree.QName(child).localname
                if tag == "Default" and (child.get("Extension") or "").lower() not in used:
                    continue
                keep.append(etree.tostring(child, method="c14n"))
            return frozenset(keep)
        if name.endswith(".rels"):
            return frozenset(etree.tostring(child, method="c14n") for child in root)
        return etree.tostring(root, method="c14n")
    except etree.C14NError:
        return raw


def diff_packages(
    before_path: str,
    after_path: str,
    *,
    ignore_parts=frozenset(),
    require_well_formed=(),
    max_differences: int = DEFAULT_MAX_DIFFERENCES,
) -> PackageDiff:
    """Compare two packages, skipping `ignore_parts` and parsing `require_well_formed`.

    `ignore_parts` is the caller's declaration of what it was allowed to
    rewrite; `require_well_formed` is the subset of those it must still be able
    to parse afterwards. A part named in `require_well_formed` that is absent
    from `after_path` makes the whole package unreadable, which is the correct
    reading -- a .docx without its body is not a document with one missing part.
    """
    diff = PackageDiff()
    differences = 0
    try:
        with zipfile.ZipFile(before_path) as before, zipfile.ZipFile(after_path) as after:
            before_names = {n for n in before.namelist() if not n.endswith("/")}
            after_names = {n for n in after.namelist() if not n.endswith("/")}

            def carries_nothing(name: str) -> bool:
                if not name.endswith(".rels"):
                    return False
                try:
                    return len(etree.fromstring(before.read(name))) == 0
                except etree.XMLSyntaxError:
                    return False

            dropped = sorted(n for n in before_names - after_names if not carries_nothing(n))
            if dropped:
                diff.dropped = dropped
                differences += 1
            added = sorted(after_names - before_names)
            if added:
                diff.added = added
                differences += 1

            for name in sorted((before_names & after_names) - set(ignore_parts)):
                if not name.endswith((".xml", ".rels")):
                    continue
                if canonical_part(name, before.read(name), before_names) != canonical_part(
                    name, after.read(name), after_names
                ):
                    diff.changed.append(name)
                    differences += 1
                    if differences >= max_differences:
                        diff.truncated = True
                        break

            for name in require_well_formed:
                try:
                    etree.fromstring(after.read(name))
                except etree.XMLSyntaxError as exc:
                    diff.malformed.append((name, str(exc)))
    except (zipfile.BadZipFile, KeyError) as exc:
        diff.unreadable = str(exc)
    return diff
