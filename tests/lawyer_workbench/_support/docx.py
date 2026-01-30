"""DOCX download + content assertions."""

from __future__ import annotations

from io import BytesIO
import zipfile
from typing import Iterable


def extract_docx_text(docx_bytes: bytes) -> str:
    """Best-effort extraction of visible text from a docx file.

    Notes:
    - Our DOCX templates use content controls (w:sdt). python-docx does not reliably
      surface w:sdtContent text, so we parse the OOXML directly.
    - For E2E assertions we only need a stable, human-visible text approximation.
    """
    if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
        return ""

    def _strip(s: str) -> str:
        return str(s or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    def _para_text(p) -> str:
        buf: list[str] = []
        for el in p.iter():
            tag = str(getattr(el, "tag", "") or "")
            if tag.endswith("}t"):
                if el.text:
                    buf.append(str(el.text))
                continue
            if tag.endswith("}tab"):
                buf.append("\t")
                continue
            if tag.endswith("}br"):
                buf.append("\n")
                continue
        return _strip("".join(buf))

    parts: list[str] = []

    try:
        with zipfile.ZipFile(BytesIO(docx_bytes)) as z:
            names = list(z.namelist())
            xml_names: list[str] = []
            for n in names:
                if n == "word/document.xml":
                    xml_names.append(n)
                elif n.startswith("word/header") and n.endswith(".xml"):
                    xml_names.append(n)
                elif n.startswith("word/footer") and n.endswith(".xml"):
                    xml_names.append(n)
                elif n in {"word/footnotes.xml", "word/endnotes.xml"}:
                    xml_names.append(n)

            import xml.etree.ElementTree as ET

            for name in xml_names:
                try:
                    root = ET.fromstring(z.read(name))
                except Exception:
                    continue
                for p in root.iter():
                    if str(getattr(p, "tag", "") or "").endswith("}p"):
                        t = _para_text(p)
                        if t:
                            parts.append(t)

            # Fallback: if there were no paragraphs, collect raw w:t (rare but harmless).
            if not parts:
                for name in xml_names:
                    try:
                        root = ET.fromstring(z.read(name))
                    except Exception:
                        continue
                    for el in root.iter():
                        if str(getattr(el, "tag", "") or "").endswith("}t") and el.text:
                            t = _strip(el.text)
                            if t:
                                parts.append(t)
    except Exception:
        return ""

    return "\n".join(parts)


def assert_docx_contains(text: str, *, must_include: Iterable[str]) -> None:
    missing: list[str] = []
    for needle in must_include:
        s = str(needle or "").strip()
        if not s:
            continue
        if s not in text:
            missing.append(s)
    if missing:
        sample = text[:2000]
        raise AssertionError(f"DOCX missing required fragments: {missing}. Extracted sample:\n{sample}")


def assert_docx_has_no_template_placeholders(text: str) -> None:
    """Catch common template placeholder leaks (jinja/docxtpl-style)."""
    t = text or ""
    bad = []
    for needle in ("{{", "}}", "{%", "%}"):
        if needle in t:
            bad.append(needle)
    if bad:
        sample = t[:2000]
        raise AssertionError(f"DOCX contains unresolved template placeholders: {bad}. Extracted sample:\n{sample}")
