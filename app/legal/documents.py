from dataclasses import dataclass
from pathlib import Path


DOCS_ROOT = Path(__file__).resolve().parent / "docs"


@dataclass(frozen=True, slots=True)
class LegalDocumentMeta:
    key: str
    title: str


@dataclass(frozen=True, slots=True)
class LegalDocumentPage:
    meta: LegalDocumentMeta
    page_number: int
    total_pages: int
    text: str


LEGAL_DOCUMENTS: tuple[LegalDocumentMeta, ...] = (
    LegalDocumentMeta(key="offer", title="Оферта"),
    LegalDocumentMeta(key="privacy", title="Политика конфиденциальности"),
    LegalDocumentMeta(key="refunds", title="Условия возврата"),
    LegalDocumentMeta(key="community_rules", title="Правила сообщества"),
)

LEGAL_DOCUMENTS_BY_KEY = {document.key: document for document in LEGAL_DOCUMENTS}


def get_legal_document_meta(document_key: str) -> LegalDocumentMeta | None:
    return LEGAL_DOCUMENTS_BY_KEY.get(document_key)


def load_legal_document_page(
    document_key: str,
    page_number: int,
    docs_root: Path = DOCS_ROOT,
) -> LegalDocumentPage | None:
    meta = get_legal_document_meta(document_key)
    if meta is None:
        return None

    page_files = sorted((docs_root / document_key).glob(f"{document_key}_[0-9][0-9].md"))
    if not page_files:
        return LegalDocumentPage(
            meta=meta,
            page_number=1,
            total_pages=1,
            text="Документ скоро будет опубликован.",
        )

    requested_page = min(max(page_number, 1), len(page_files))
    text = page_files[requested_page - 1].read_text(encoding="utf-8").strip()
    return LegalDocumentPage(
        meta=meta,
        page_number=requested_page,
        total_pages=len(page_files),
        text=text,
    )


def render_legal_document_page(page: LegalDocumentPage) -> str:
    return f"📄 {page.meta.title}\nСтраница {page.page_number} из {page.total_pages}\n\n{page.text}"
