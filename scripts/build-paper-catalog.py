#!/usr/bin/env python3
"""Build the unlisted paper catalogue from reviewed mirrors and LaTeX citations.

The script has no third-party dependencies. It reads only writing sources
allowlisted in papers/catalog-sources.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
PAPERS_DIR = REPO_ROOT / "papers"
CONFIG_PATH = PAPERS_DIR / "catalog-sources.json"
AUTHORED_PATH = PAPERS_DIR / "authored" / "manifest.json"
LATTICE_PATH = PAPERS_DIR / "collections" / "lattice-zk.json"
OUTPUT_PATH = PAPERS_DIR / "manifest.json"
BASE_URL = "https://quangvdao.github.io/papers"

CITE_RE = re.compile(
    r"\\(?:[A-Za-z]*cite[A-Za-z*]*|nocite)"
    r"(?:\s*\[[^\]]*\])*\s*\{([^}]*)\}",
    re.DOTALL,
)
INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{([^}]+)\}")
ADD_BIB_RE = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\s*\{([^}]+)\}")
URL_RE = re.compile(r"https?://[^\s{}\"<>]+", re.IGNORECASE)
EPRINT_URL_RE = re.compile(r"eprint\.iacr\.org/(?:eprint/)?(\d{4})/(\d+)", re.IGNORECASE)
EPRINT_TEXT_RE = re.compile(
    r"(?:cryptology\s+eprint|iacr\s+eprint|eprint\s+archive)"
    r".{0,80}?(?:paper|report)?\s*(\d{4})\s*[/:-]\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)
ARXIV_RE = re.compile(
    r"(?:arxiv(?:\.org/(?:abs|pdf)/|:)\s*)([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s{}\"<>]+)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})


def strip_tex_comments(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        match = re.search(r"(?<!\\)%", line)
        cleaned.append(line[: match.start()] if match else line)
    return "\n".join(cleaned)


def resolve_tex_path(candidate: str, current_dir: Path, root: Path) -> Path | None:
    candidate = candidate.strip()
    names = [candidate] if candidate.endswith(".tex") else [candidate, f"{candidate}.tex"]
    for base in (current_dir, root):
        for name in names:
            path = (base / name).resolve()
            if path.is_file():
                return path
    return None


def scan_tex_closure(root: Path, entry: str) -> dict[str, Any]:
    entry_path = resolve_tex_path(entry, root, root)
    if entry_path is None:
        return {
            "status": "entry-missing",
            "files": [],
            "citation_keys": [],
            "bibliographies": [],
        }

    seen: set[Path] = set()
    citation_keys: set[str] = set()
    bibliographies: set[Path] = set()

    def walk(path: Path) -> None:
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        text = strip_tex_comments(path.read_text(encoding="utf-8", errors="replace"))

        for match in CITE_RE.finditer(text):
            for key in match.group(1).split(","):
                key = key.strip()
                if key and key != "*":
                    citation_keys.add(key)

        for regex in (BIBLIOGRAPHY_RE, ADD_BIB_RE):
            for match in regex.finditer(text):
                for raw_name in match.group(1).split(","):
                    name = raw_name.strip()
                    if not name:
                        continue
                    if not name.endswith(".bib"):
                        name += ".bib"
                    candidates = [(path.parent / name).resolve(), (root / name).resolve()]
                    bib_path = next((item for item in candidates if item.is_file()), None)
                    if bib_path:
                        bibliographies.add(bib_path)

        for match in INPUT_RE.finditer(text):
            child = resolve_tex_path(match.group(1), path.parent, root)
            if child:
                walk(child)

    walk(entry_path)
    return {
        "status": "processed",
        "files": sorted(seen),
        "citation_keys": sorted(citation_keys),
        "bibliographies": sorted(bibliographies),
    }


def find_balanced_end(text: str, opening_index: int, opening: str) -> int | None:
    closing = "}" if opening == "{" else ")"
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening_index, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    brace_depth = 0
    paren_depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth = max(0, paren_depth - 1)
        elif char == delimiter and brace_depth == 0 and paren_depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def unwrap_bib_value(value: str) -> str:
    pieces = split_top_level(value, "#")
    unwrapped: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        while len(piece) >= 2 and (
            (piece[0] == "{" and piece[-1] == "}")
            or (piece[0] == '"' and piece[-1] == '"')
        ):
            piece = piece[1:-1].strip()
        unwrapped.append(piece)
    return " ".join(unwrapped).strip()


def parse_bibtex(path: Path) -> dict[str, dict[str, str]]:
    text = strip_tex_comments(path.read_text(encoding="utf-8", errors="replace"))
    entries: dict[str, dict[str, str]] = {}
    cursor = 0
    header_re = re.compile(r"@([A-Za-z]+)\s*([\{\(])")
    while True:
        match = header_re.search(text, cursor)
        if not match:
            break
        entry_type = match.group(1).lower()
        opening = match.group(2)
        opening_index = match.end() - 1
        end = find_balanced_end(text, opening_index, opening)
        if end is None:
            break
        cursor = end + 1
        if entry_type in {"comment", "preamble", "string"}:
            continue

        body = text[opening_index + 1 : end]
        head = split_top_level(body, ",")
        if len(head) < 2:
            continue
        key = head[0].strip()
        fields: dict[str, str] = {"entry_type": entry_type, "bibtex_key": key}
        for item in head[1:]:
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip().lower()
            if name:
                fields[name] = unwrap_bib_value(value)
        current = entries.get(key)
        if current is None or metadata_score(fields) > metadata_score(current):
            entries[key] = fields
    return entries


def metadata_score(fields: dict[str, str]) -> int:
    preferred = ("title", "author", "year", "url", "doi", "eprint", "howpublished")
    return sum(2 if fields.get(field) else 0 for field in preferred) + len(fields)


ACCENTS = {
    ("'", "a"): "á",
    ("'", "e"): "é",
    ("'", "i"): "í",
    ("'", "o"): "ó",
    ("'", "u"): "ú",
    ('"', "a"): "ä",
    ('"', "e"): "ë",
    ('"', "i"): "ï",
    ('"', "o"): "ö",
    ('"', "u"): "ü",
    ("`", "a"): "à",
    ("`", "e"): "è",
    ("^", "a"): "â",
    ("^", "e"): "ê",
    ("^", "i"): "î",
    ("^", "o"): "ô",
    ("^", "u"): "û",
    ("~", "n"): "ñ",
}


def clean_tex(value: str) -> str:
    value = unquote(value or "")
    value = re.sub(r"\\url\s*\{([^}]*)\}", r"\1", value)
    value = re.sub(r"\\href\s*\{([^}]*)\}\s*\{([^}]*)\}", r"\2", value)
    value = value.replace(r"\textasciitilde", "~")
    value = value.replace(r"\lambda", "λ")
    value = value.replace(r"\#", "#").replace(r"\(", "").replace(r"\)", "")
    value = re.sub(
        r"\{?\\([\"'`~^])\{?([A-Za-z])\}?\}?",
        lambda match: ACCENTS.get(
            (match.group(1), match.group(2).lower()), match.group(2)
        ),
        value,
    )
    value = re.sub(r"\{?\\ss\}?", "ß", value)
    value = re.sub(r"\{?\\c\s*\{?c\}?\}?", "ç", value, flags=re.IGNORECASE)
    value = value.replace(r"\&", "&").replace(r"\_", "_").replace("~", " ")
    value = re.sub(r"\\(?:textsc|textit|textbf|emph|mathrm|mathsf)\s*\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\[A-Za-z]+\*?", "", value)
    value = value.replace("{", "").replace("}", "").replace("$", "")
    value = re.sub(r"\s+", " ", value).strip(" ,")
    return value


def normalize_doi(value: str) -> str | None:
    value = clean_tex(value).strip()
    match = re.search(r"(10\.\d{4,9}/\S+)", value, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).rstrip(".,);]").lower()


def identifiers_from_fields(fields: dict[str, str]) -> dict[str, str]:
    combined = " ".join(fields.values())
    identifiers: dict[str, str] = {}

    doi = normalize_doi(fields.get("doi", ""))
    if not doi:
        match = DOI_RE.search(clean_tex(combined))
        doi = normalize_doi(match.group(1)) if match else None
    if doi:
        identifiers["doi"] = doi

    match = EPRINT_URL_RE.search(clean_tex(combined)) or EPRINT_TEXT_RE.search(combined)
    if match:
        identifiers["eprint"] = f"{match.group(1)}/{int(match.group(2))}"
    elif fields.get("eprint") and "arxiv" not in fields.get("archiveprefix", "").lower():
        raw = clean_tex(fields["eprint"])
        match = re.fullmatch(r"(\d{4})\s*[/:-]\s*(\d+)", raw)
        if match:
            identifiers["eprint"] = f"{match.group(1)}/{int(match.group(2))}"

    match = ARXIV_RE.search(clean_tex(combined))
    if match:
        identifiers["arxiv"] = match.group(1).lower()
    elif fields.get("eprint") and "arxiv" in fields.get("archiveprefix", "").lower():
        identifiers["arxiv"] = clean_tex(fields["eprint"]).lower()
    return identifiers


def canonical_id(identifiers: dict[str, str], title: str, fallback: str) -> str:
    for kind in ("eprint", "doi", "arxiv"):
        if identifiers.get(kind):
            return f"{kind}:{identifiers[kind]}"
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title).encode(
        "ascii", "ignore"
    ).decode("ascii").lower()).strip("-")
    if slug:
        return f"title:{slug[:120]}"
    digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:16]
    return f"bib:{digest}"


def source_url_from_fields(fields: dict[str, str], identifiers: dict[str, str]) -> str | None:
    if identifiers.get("eprint"):
        return f"https://eprint.iacr.org/{identifiers['eprint']}"
    if identifiers.get("arxiv"):
        return f"https://arxiv.org/abs/{identifiers['arxiv']}"
    if identifiers.get("doi"):
        return f"https://doi.org/{identifiers['doi']}"
    for field in ("url", "howpublished", "note", "ee"):
        value = clean_tex(fields.get(field, ""))
        match = URL_RE.search(value)
        if match:
            return match.group(0).rstrip(".,);]")
    return None


def authors_from_field(value: str) -> list[str]:
    if not value:
        return []
    return [clean_tex(author) for author in re.split(r"\s+and\s+", value) if clean_tex(author)]


def citation_record(
    fields: dict[str, str],
    citation_key: str,
    source_id: str,
    collections: list[str],
    bib_path: Path,
) -> dict[str, Any]:
    title = clean_tex(fields.get("title", "")) or citation_key
    identifiers = identifiers_from_fields(fields)
    record: dict[str, Any] = {
        "id": canonical_id(identifiers, title, f"{source_id}:{citation_key}"),
        "title": title,
        "kind": fields.get("entry_type", "misc"),
        "relations": ["cited"],
        "collections": sorted_unique(collections),
        "cited_by": [source_id],
        "citation_keys": [citation_key],
        "availability": "source-only" if source_url_from_fields(fields, identifiers) else "metadata-only",
        "license_status": "not-reviewed",
        "provenance": {
            "writing_sources": [source_id],
            "bibliography_files": [str(bib_path.relative_to(bib_path.parents[1]))],
        },
    }
    authors = authors_from_field(fields.get("author", ""))
    if authors:
        record["authors"] = authors
    year_match = YEAR_RE.search(clean_tex(fields.get("year", "")))
    if year_match:
        record["year"] = int(year_match.group(0))
    if identifiers:
        record["identifiers"] = identifiers
    source_url = source_url_from_fields(fields, identifiers)
    if source_url:
        record["source_url"] = source_url
    return record


def title_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", normalized.lower())


def merge_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    for field in ("relations", "collections", "cited_by", "citation_keys"):
        existing[field] = sorted_unique(existing.get(field, []) + incoming.get(field, []))
        if not existing[field]:
            existing.pop(field, None)

    if incoming.get("identifiers"):
        existing.setdefault("identifiers", {}).update(incoming["identifiers"])
    if incoming.get("authors") and not existing.get("authors"):
        existing["authors"] = incoming["authors"]
    for field in ("year", "source_url", "kind"):
        if incoming.get(field) and not existing.get(field):
            existing[field] = incoming[field]

    incoming_provenance = incoming.get("provenance", {})
    if incoming_provenance:
        provenance = existing.setdefault("provenance", {})
        for field in ("writing_sources", "bibliography_files"):
            provenance[field] = sorted_unique(
                provenance.get(field, []) + incoming_provenance.get(field, [])
            )
            if not provenance[field]:
                provenance.pop(field, None)
    return existing


def record_from_authored(item: dict[str, Any]) -> dict[str, Any]:
    raw_id = item["id"]
    if raw_id.lower().startswith("arxiv:"):
        identifiers = {"arxiv": raw_id.split(":", 1)[1].lower()}
    else:
        identifiers = {"eprint": raw_id}
    record: dict[str, Any] = {
        "id": canonical_id(identifiers, item["title"], raw_id),
        "title": item["title"],
        "kind": "paper",
        "relations": ["authored"],
        "collections": ["authored"],
        "availability": "mirrored",
        "license_status": "not-recorded",
        "identifiers": identifiers,
        "source_url": item["source_url"],
        "mirror_url": item["mirror_url"],
        "artifact_basis": item["artifact_basis"],
        "pages": item["pages"],
        "bytes": item["bytes"],
        "sha256": item["sha256"],
        "provenance": {"collection_manifest": "papers/authored/manifest.json"},
    }
    if item.get("alternate_versions"):
        record["alternate_versions"] = item["alternate_versions"]
    match = YEAR_RE.search(raw_id)
    if match:
        record["year"] = int(match.group(0))
    return record


def slugify_collection(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def record_from_lattice(item: dict[str, Any]) -> dict[str, Any]:
    identifiers = {"eprint": item["eprint_id"]}
    category = slugify_collection(item["category"])
    record: dict[str, Any] = {
        "id": canonical_id(identifiers, item["title"], item["eprint_id"]),
        "title": item["title"],
        "authors": item["authors"],
        "year": item["year"],
        "kind": "paper",
        "relations": ["curated"],
        "collections": sorted_unique(["lattice-zk", category]),
        "tags": item.get("tags", []),
        "availability": "mirrored",
        "license_status": "reviewed",
        "identifiers": identifiers,
        "source_url": item["source_url"],
        "mirror_url": item["mirror_url"],
        "license": item["license"],
        "license_url": item["license_url"],
        "modified": item["modified"],
        "pages": item["pages"],
        "bytes": item["bytes"],
        "sha256": item["sha256"],
        "provenance": {"collection_manifest": "papers/collections/lattice-zk.json"},
    }
    return record


def build_catalog(research_root_override: Path | None = None) -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    authored = load_json(AUTHORED_PATH)
    lattice = load_json(LATTICE_PATH)
    configured_root = Path(config["research_root"]).expanduser()
    research_root = research_root_override or configured_root

    records: dict[str, dict[str, Any]] = {}
    titles: dict[str, str] = {}

    def add(record: dict[str, Any]) -> None:
        identity = record["id"]
        normalized_title = title_key(record["title"])
        existing_id = identity if identity in records else titles.get(normalized_title)
        if existing_id:
            merge_records(records[existing_id], record)
            return
        records[identity] = record
        if normalized_title:
            titles[normalized_title] = identity

    for item in authored["papers"]:
        add(record_from_authored(item))
    for item in lattice["papers"]:
        add(record_from_lattice(item))

    source_reports: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []

    for source in config["sources"]:
        root = research_root / source["folder"]
        report: dict[str, Any] = {
            "id": source["id"],
            "folder": source["folder"],
            "entry": source["entry"],
        }
        if not root.is_dir():
            report["status"] = "folder-missing"
            source_reports.append(report)
            continue

        scan = scan_tex_closure(root, source["entry"])
        report["status"] = scan["status"]
        if scan["status"] != "processed":
            source_reports.append(report)
            continue

        bib_entries: dict[str, tuple[dict[str, str], Path]] = {}
        for bib_path in scan["bibliographies"]:
            for key, entry in parse_bibtex(bib_path).items():
                current = bib_entries.get(key)
                if current is None or metadata_score(entry) > metadata_score(current[0]):
                    bib_entries[key] = (entry, bib_path)

        resolved_count = 0
        for key in scan["citation_keys"]:
            resolved = bib_entries.get(key)
            if not resolved:
                unresolved.append({"writing": source["id"], "citation_key": key})
                continue
            fields, bib_path = resolved
            add(citation_record(fields, key, source["id"], source["collections"], bib_path))
            resolved_count += 1

        report.update(
            {
                "files_scanned": len(scan["files"]),
                "bibliography_files": [
                    str(path.relative_to(research_root)) for path in scan["bibliographies"]
                ],
                "citation_keys": len(scan["citation_keys"]),
                "resolved_citation_keys": resolved_count,
                "unresolved_citation_keys": len(scan["citation_keys"]) - resolved_count,
            }
        )
        source_reports.append(report)

    papers = sorted(
        records.values(),
        key=lambda item: (
            0 if "authored" in item.get("relations", []) else 1,
            -(item.get("year") or 0),
            item["title"].casefold(),
        ),
    )

    collection_descriptions = {
        "authored": "Papers authored or co-authored by Quang Dao and linked from the homepage.",
        "citation-closure": "Works cited by the currently resolvable allowlisted writing sources.",
        "snarks": "General SNARK, succinct-argument, polynomial-protocol, and zkVM references.",
        "formal-verification": "Formal verification and mechanized cryptographic-proof references.",
        "lattice-zk": "Lattice-based proof systems, polynomial commitments, folding, and applications.",
        "pcs": "Polynomial commitment schemes.",
        "sumcheck": "Sum-check protocols and prover optimizations.",
        "zkvm": "Zero-knowledge virtual machines and their proof systems.",
        "post-quantum-cryptography": "Post-quantum cryptography outside the lattice proof-system pilot.",
        "foundations": "Foundational lattice proof-system papers.",
        "folding": "Lattice folding schemes and related recursive arguments.",
        "polynomial-commitments": "Lattice-based polynomial commitments.",
        "succinct-arguments": "Lattice-based succinct arguments.",
        "applications-and-tools": "Applications and tooling for lattice proof systems.",
    }
    collection_counts: dict[str, int] = defaultdict(int)
    for paper in papers:
        for collection in paper.get("collections", []):
            collection_counts[collection] += 1
    collections = [
        {
            "id": collection,
            "description": collection_descriptions.get(
                collection, collection.replace("-", " ").capitalize() + "."
            ),
            "paper_count": collection_counts[collection],
        }
        for collection in sorted(collection_counts)
    ]

    availability_counts: dict[str, int] = defaultdict(int)
    relation_counts: dict[str, int] = defaultdict(int)
    for paper in papers:
        availability_counts[paper["availability"]] += 1
        for relation in paper.get("relations", []):
            relation_counts[relation] += 1

    processed_sources = [report for report in source_reports if report["status"] == "processed"]
    missing_sources = [report for report in source_reports if report["status"] != "processed"]
    citation_key_total = sum(report.get("citation_keys", 0) for report in processed_sources)
    citation_key_union = len(
        {
            key
            for paper in papers
            for key in paper.get("citation_keys", [])
        }
        | {item["citation_key"] for item in unresolved}
    )

    return {
        "schema_version": "2.0",
        "name": "Quang Dao — Paper Mirror and Citation Catalogue",
        "description": (
            "A machine-readable catalogue of authored papers, an unlisted external-paper "
            "mirror, and the citation closure available from allowlisted writing sources."
        ),
        "updated": config.get("updated", date.today().isoformat()),
        "schema_url": f"{BASE_URL}/manifest.schema.json",
        "visibility": {
            "authored": "homepage-linked",
            "external_library": "unlisted",
            "note": (
                "Unlisted is not access control. Anyone with a paper or manifest URL can "
                "retrieve it. The external library is not linked from the human-facing site."
            ),
        },
        "agent_entrypoint": f"{BASE_URL}/manifest.json",
        "agent_instructions": f"{BASE_URL}/AGENTS.md",
        "collection_license_note": (
            "No blanket license is asserted over the catalogue. Read each paper's license "
            "fields. Absence of a reviewed license is not permission to redistribute."
        ),
        "availability_definitions": {
            "mirrored": "A self-hosted PDF is available at mirror_url.",
            "source-only": "A canonical source URL is recorded, but no self-hosted PDF is provided.",
            "metadata-only": "Citation metadata is recorded, but no reliable public source URL was resolved.",
        },
        "collections": collections,
        "coverage": {
            "catalog_records": len(papers),
            "by_relation": dict(sorted(relation_counts.items())),
            "by_availability": dict(sorted(availability_counts.items())),
            "citation_sources_processed": len(processed_sources),
            "citation_sources_missing": len(missing_sources),
            "citation_keys_across_sources": citation_key_total,
            "distinct_citation_keys": citation_key_union,
            "source_reports": source_reports,
            "unresolved_citations": sorted(
                unresolved, key=lambda item: (item["writing"], item["citation_key"])
            ),
            "scope_note": (
                "Citation closure covers only source trees listed in catalog-sources.json "
                "that resolve on this machine. It does not yet cover every paper on the homepage."
            ),
        },
        "papers": papers,
    }


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    for index, paper in enumerate(catalog.get("papers", [])):
        prefix = f"papers[{index}]"
        for required in ("id", "title", "relations", "collections", "availability"):
            if not paper.get(required):
                errors.append(f"{prefix}: missing {required}")
        if paper.get("id") in ids:
            errors.append(f"{prefix}: duplicate id {paper['id']}")
        ids.add(paper.get("id"))
        if paper.get("availability") == "mirrored":
            for required in ("mirror_url", "sha256", "bytes", "pages"):
                if not paper.get(required):
                    errors.append(f"{prefix}: mirrored record missing {required}")
            mirror_url = paper.get("mirror_url", "")
            marker = "https://quangvdao.github.io/"
            if mirror_url.startswith(marker):
                local_path = REPO_ROOT / mirror_url[len(marker) :]
                if not local_path.is_file():
                    errors.append(f"{prefix}: mirror file does not exist: {local_path}")
                else:
                    data = local_path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != paper.get("sha256"):
                        errors.append(f"{prefix}: SHA-256 mismatch")
                    if len(data) != paper.get("bytes"):
                        errors.append(f"{prefix}: byte-size mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-root",
        type=Path,
        help="Override the research root configured in papers/catalog-sources.json.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate inputs and fail if papers/manifest.json is not current.",
    )
    args = parser.parse_args()

    catalog = build_catalog(args.research_root)
    errors = validate_catalog(catalog)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    rendered = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT_PATH.is_file() or OUTPUT_PATH.read_text(encoding="utf-8") != rendered:
            print("error: papers/manifest.json is out of date", file=sys.stderr)
            return 1
        print(
            f"catalog valid: {len(catalog['papers'])} records; "
            f"{catalog['coverage']['distinct_citation_keys']} citation keys"
        )
        return 0

    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}: {len(catalog['papers'])} records; "
        f"{catalog['coverage']['distinct_citation_keys']} citation keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
