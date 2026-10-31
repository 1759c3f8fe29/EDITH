"""Writing documents NEXUS can also read back (spec §5, O15).

Four formats, three of them built on the standard library alone. ``requirements.txt``
is fastapi / uvicorn / pydantic / httpx / sse-starlette / keyring / python-multipart
-- no python-docx, no python-pptx, no openpyxl, no reportlab. OOXML is zip + XML and
PDF is a text format with a byte-offset table, so both can be written correctly
without adding a dependency, which is why they are.

The module is split along the seam that actually exists rather than along the one
the plan first drew: :mod:`blocks` is the document model every writer consumes,
:mod:`ooxml` writes the three zip-based Office formats, and :mod:`pdf` writes the
one format that is not OOXML. Putting the PDF writer in a file called ``ooxml``
would have been one less file and one more thing to mislead a reader with.

Nothing here pretends. A file that is written but not verified is reported as
unverified; see :mod:`verify` for the read-back that turns a claim into evidence.
"""

from __future__ import annotations

from .blocks import KINDS, Block, blocks_from, describe
from .ooxml import write_docx, write_pptx, write_xlsx
from .pdf import write_pdf
from .verify import verify_bytes, verify_document

__all__ = [
    "KINDS",
    "Block",
    "blocks_from",
    "describe",
    "verify_bytes",
    "verify_document",
    "write_docx",
    "write_pdf",
    "write_pptx",
    "write_xlsx",
]
